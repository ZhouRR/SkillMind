"""Run 内の監査済み構造観測と明示された読取訂正を、元の binding に結び付ける。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.postgres_schema import validate_table_schema
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import AgentTaskBriefSnapshot, Evidence, RunSegment, ToolCall

if TYPE_CHECKING:
    from skillmind.agent.run_binding import BoundRunResource
    from skillmind.agent.tool_gateway import RunToolContext


def database_audit_identity(
    arguments: Mapping[str, Any], binding_id: UUID | None
) -> dict[str, Any]:
    """値を残さず、同条件の読取頻度と明示 recovery の相関に必要な識別子を固定する。"""
    query = {
        k: v for k, v in arguments.items() if k not in {"purpose", "recovery", "recovery_from"}
    }
    table = arguments.get("table")
    if not isinstance(table, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$]{0,62}\.[A-Za-z_][A-Za-z0-9_$]{0,62}", table
    ):
        table = None
    result: dict[str, Any] = {
        "version": "v1",
        "table": table,
        "binding_id": str(binding_id),
        "query_checksum": "sha256:" + sha256_hex(canonical_json(query)),
    }
    if arguments.get("refresh") is True:
        result["refresh"] = True
    recovery = arguments.get("recovery")
    failed_id = (
        recovery.get("failed_tool_call_id")
        if isinstance(recovery, Mapping)
        else arguments.get("recovery_from")
    )
    if isinstance(failed_id, str):
        with suppress(ValueError):
            result["recovery_from"] = str(UUID(failed_id))
    schema_ref = recovery.get("schema_evidence_ref") if isinstance(recovery, Mapping) else None
    if isinstance(schema_ref, str) and re.fullmatch(r"ev_[a-zA-Z0-9_-]{1,150}", schema_ref):
        result["schema_evidence_ref"] = schema_ref
    return result


class DatabaseObservations:
    """既存 ToolCall/Evidence を正本にし、追加の cache service や業務状態を作らない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Provider と同じアプリ DB の session factory を共有する。"""
        self._sessions = session_factory

    async def lookup(
        self,
        context: RunToolContext,
        bound: BoundRunResource,
        table: str,
        *,
        before: datetime | None = None,
    ) -> dict[str, Any] | None:
        """監査が欠けた場合は miss。取得時点・元 Evidence を変更せず返す。"""
        try:
            async with self._sessions() as session:
                records = (
                    await session.execute(
                        select(Evidence, ToolCall)
                        .join(ToolCall, Evidence.tool_call_id == ToolCall.id)
                        .where(
                            Evidence.run_id == context.run_id,
                            ToolCall.run_id == context.run_id,
                            ToolCall.integration_id == context.tool.integration_id,
                            ToolCall.status == "SUCCEEDED",
                            Evidence.metadata_json["binding_checksum"].as_string()
                            == bound.checksum,
                            Evidence.metadata_json["binding_id"].as_string()
                            == str(context.tool.binding_id),
                            ToolCall.result_json["table"].as_string() == table,
                            func.jsonb_typeof(ToolCall.result_json["table_schema"]) == "object",
                            ToolCall.capability_version.in_(
                                ["database.describe/v1", "database.read/v2"]
                            ),
                        )
                        .where(Evidence.created_at <= before if before else true())
                        .order_by(Evidence.created_at.desc(), Evidence.id.desc())
                        .limit(1)
                    )
                ).all()
                for evidence, call in records:
                    metadata = evidence.metadata_json or {}
                    if metadata.get("binding_checksum") != bound.checksum or metadata.get(
                        "binding_id"
                    ) != str(context.tool.binding_id):
                        continue
                    value = call.result_json or {}
                    if (
                        value.get("status") != "success"
                        or value.get("table") != table
                        or "table_schema" not in value
                    ):
                        continue
                    schema = validate_table_schema(value["table_schema"])
                    checksum = "sha256:" + sha256_hex(canonical_json(schema))
                    if call.capability_version == "database.describe/v1":
                        if value.get("schema_checksum") != checksum:
                            continue
                        if evidence.content_hash != checksum:
                            continue
                        observed_at = value["observed_at"]
                        refs = value.get("observation_refs") or [evidence.evidence_ref]
                    else:
                        content = {
                            k: value[k]
                            for k in ("table", "rows", "read_at", "truncated", "table_schema")
                        }
                        if value.get("content_hash") != "sha256:" + sha256_hex(
                            canonical_json(content)
                        ):
                            continue
                        if evidence.content_hash != value["content_hash"]:
                            continue
                        observed_at, refs = value["read_at"], [evidence.evidence_ref]
                    datetime.fromisoformat(observed_at)
                    if (
                        not isinstance(refs, list)
                        or not refs
                        or not all(isinstance(ref, str) and ref.startswith("ev_") for ref in refs)
                    ):
                        continue
                    return {
                        "table_schema": schema,
                        "schema_checksum": checksum,
                        "observed_at": observed_at,
                        "observation_refs": refs,
                    }
        except (SQLAlchemyError, OSError, ValueError, KeyError, TypeError, AttributeError):
            # 派生 cache の障害で通常の外部読取を止めない。取消は捕捉しない。
            return None
        return None

    async def recovery(
        self, context: RunToolContext, table: str, failed_id: str, schema_ref: str | None = None
    ) -> None:
        """別 Run/表/権限の参照や二度目の訂正を拒否し、成功を推測で関連付けない。"""
        failed_id = str(UUID(failed_id))
        async with self._sessions() as session:
            original = await session.get(ToolCall, UUID(failed_id))
            identity = original.arguments_summary.get("database", {}) if original else {}
            diagnostic = (original.error_json or {}).get("database", {}) if original else {}
            if (
                original is None
                or original.run_id != context.run_id
                or original.integration_id != context.tool.integration_id
                or original.status != "FAILED"
                or original.capability_version != "database.read/v2"
                or identity.get("table") != table
                or identity.get("binding_id") != str(context.tool.binding_id)
                or identity.get("recovery_from")
                or diagnostic.get("reason_code")
                not in {"undefined_column", "parameter_type_mismatch"}
                or diagnostic.get("stage") != "row_read"
            ):
                raise ValueError("Recovery requires this Run's original correctable read failure")
            others = await session.scalar(
                select(ToolCall.id)
                .where(
                    ToolCall.run_id == context.run_id,
                    ToolCall.capability_version == context.tool.capability,
                    ToolCall.arguments_summary["database"]["recovery_from"].as_string()
                    == failed_id,
                    ToolCall.id != context.tool_call_id,
                )
                .limit(1)
            )
            if others is not None:
                raise ValueError(
                    "One schema inspection and correction is allowed per original failure"
                )
            if schema_ref:
                observed = (
                    await session.execute(
                        select(Evidence, ToolCall)
                        .join(ToolCall, Evidence.tool_call_id == ToolCall.id)
                        .where(
                            Evidence.run_id == context.run_id,
                            ToolCall.run_id == context.run_id,
                            Evidence.evidence_ref == schema_ref,
                            ToolCall.status == "SUCCEEDED",
                            ToolCall.capability_version == "database.describe/v1",
                        )
                    )
                ).one_or_none()
                if observed is None:
                    raise ValueError("Recovery requires the linked schema inspection evidence")
                evidence, call = observed
                schema_identity = call.arguments_summary.get("database", {})
                if (
                    schema_identity.get("recovery_from") != failed_id
                    or schema_identity.get("binding_id") != str(context.tool.binding_id)
                    or schema_identity.get("table") != table
                    or call.integration_id != context.tool.integration_id
                    or evidence.created_at < original.created_at
                ):
                    raise ValueError("Schema evidence does not belong to this recovery")

    async def segment_index(
        self, run_id: UUID, segment_id: UUID
    ) -> tuple[list[dict[str, Any]] | None, datetime]:
        """同 Segment の固定済み投影を優先し、新 Segment は作成前の観測だけを採用する。"""
        async with self._sessions() as session:
            segment = await session.get(RunSegment, segment_id)
            if segment is None or segment.run_id != run_id:
                raise ValueError("Schema projection requires the current Run segment")
            frozen = await session.scalar(
                select(AgentTaskBriefSnapshot).where(
                    AgentTaskBriefSnapshot.run_id == run_id,
                    AgentTaskBriefSnapshot.run_segment_id == segment_id,
                )
            )
            if frozen is not None:
                return frozen.brief_json.get("checkpoint", {}).get(
                    "database_observations", []
                ), segment.created_at
            return None, segment.created_at

    async def tables(self, context: RunToolContext, before: datetime) -> list[str]:
        """候補名のみ読み、同一 Run/binding でも過去の行本文を一括取得しない。"""
        table_name = ToolCall.result_json["table"].as_string()
        async with self._sessions() as session:
            names = await session.scalars(
                select(table_name)
                .where(
                    ToolCall.run_id == context.run_id,
                    ToolCall.integration_id == context.tool.integration_id,
                    ToolCall.capability_version.in_(["database.describe/v1", "database.read/v2"]),
                    ToolCall.status == "SUCCEEDED",
                    ToolCall.created_at <= before,
                    ToolCall.arguments_summary["database"]["binding_id"].as_string()
                    == str(context.tool.binding_id),
                    func.jsonb_typeof(ToolCall.result_json["table_schema"]) == "object",
                )
                .group_by(table_name)
                .order_by(func.max(ToolCall.created_at).desc(), table_name)
                .limit(20)
            )
            return [name for name in names if isinstance(name, str)]
