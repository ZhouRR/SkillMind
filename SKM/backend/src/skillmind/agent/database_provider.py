"""Run の固定 binding を使う PostgreSQL database.read/v1 Provider。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.database_observations import DatabaseObservations
from skillmind.agent.domain import RegisteredTool, RunWorkspace
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.postgres_schema import validate_table_schema
from skillmind.agent.postgres_source import (
    MAX_DATABASE_BYTES,
    DatabaseReadError,
    DatabaseSource,
    build_database_query,
)
from skillmind.agent.run_binding import (
    BoundRunResource,
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.agent.runtime_policy import runtime_policy
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.timing import safe_observation
from skillmind.effects.database_write import database_row_revision
from skillmind.integrations.domain import scope_values_allow
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.runs.domain import ClaimedRun


class DatabaseReadProvider:
    """モデルへ接続設定を渡さず、原テーブル範囲内の結果だけを証拠化する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source: DatabaseSource,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """権限 DB、外部読取 source と Secret resolver を注入する。"""
        self._session_factory = session_factory
        self._source = source
        self._secret_resolver = secret_resolver
        self._observations = DatabaseObservations(session_factory)

    async def _bound(self, context: RunToolContext) -> tuple[BoundRunResource, str]:
        """I/O の前後で同じ Run binding と現在の Secret 状態を検証する。"""
        if context.tool.integration_id is None or context.tool.binding_id is None:
            raise ToolProviderError(
                "invalid_request", "Database binding is invalid", retryable=False
            )
        try:
            async with self._session_factory() as session:
                bound = await load_bound_run_resource(
                    session,
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=context.tool.binding_id,
                    integration_id=context.tool.integration_id,
                    provider="postgres",
                    capability="database.read/v1",
                )
                password = await resolve_binding_secret(
                    session,
                    resolver=self._secret_resolver,
                    integration=bound.integration,
                    required=True,
                )
        except RunBindingError as error:
            raise ToolProviderError(error.code, error.message, retryable=error.retryable) from None
        if password is None:
            raise ToolProviderError(
                "unavailable", "Database credential is unavailable", retryable=False
            )
        return bound, password

    async def execute(
        self,
        context: RunToolContext,
        arguments: Mapping[str, Any],
    ) -> ProviderToolResult:
        """明示テーブルを読み、撤権後や版変更後の結果を公開しない。"""
        modern = bool(context.run and runtime_policy(context.run.task_brief or {}))
        if context.tool.capability in {"database.read/v2", "database.describe/v1"} and not modern:
            raise ToolProviderError(
                "invalid_request", "Tool requires the frozen runtime policy", retryable=False
            )
        if context.tool.capability == "database.describe/v1":
            return await self._describe(context, arguments)
        recovery = arguments.get("recovery") if modern else None
        query_arguments = (
            {k: v for k, v in arguments.items() if k != "recovery"} if modern else arguments
        )
        try:
            query = build_database_query(query_arguments)
        except ValueError:
            raise ToolProviderError(
                "invalid_request", "Database read request is invalid", retryable=False
            ) from None
        bound, password = await self._bound(context)
        if not scope_values_allow(bound.scope.get("tables", []), query.table):
            raise ToolProviderError(
                "scope_denied", "Table is outside the frozen scope", retryable=False
            )
        if recovery is not None:
            await self._validate_recovery(context, query.table, recovery)
        try:
            rows = await self._source.read(bound.integration.config, password, query)
        except DatabaseReadError as error:
            await self._same_bound(context, bound, password)
            diagnostic = error.diagnostic
            safe_observation(
                "database.read.failed",
                run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                tool_call_id=context.tool_call_id,
                reason_code=diagnostic.reason_code,
                sqlstate=diagnostic.sqlstate,
                failure_stage=diagnostic.stage,
            )
            if modern:
                raise ToolProviderError(
                    diagnostic.code,
                    diagnostic.message,
                    retryable=False,
                    diagnostic={
                        "reason_code": diagnostic.reason_code,
                        "stage": diagnostic.stage,
                        "sqlstate": diagnostic.sqlstate,
                    },
                ) from None
            raise ToolProviderError(
                "unavailable", "Database read could not be completed", retryable=False
            ) from None
        current, current_password = await self._bound(context)
        if current != bound or current_password != password:
            raise ToolProviderError(
                "unavailable", "Database binding changed during read", retryable=False
            )
        table_schema = None
        if query.include_schema:
            try:
                table_schema = validate_table_schema(rows.table_schema)
            except ValueError:
                raise ToolProviderError(
                    "unavailable", "Database schema could not be confirmed", retryable=False
                ) from None
        schema_bytes = len(canonical_json(table_schema).encode("utf-8")) if table_schema else 0
        # SQL の出力制限とは別に、注入 source からの結果も公開前に有界性を検証する。
        if (
            len(rows.rows) > query.limit
            or len(canonical_json(rows.rows).encode("utf-8")) + schema_bytes > MAX_DATABASE_BYTES
        ):
            raise ToolProviderError(
                "unavailable", "Database result exceeds the read limit", retryable=False
            )
        read_at = datetime.now(UTC).isoformat()
        row_hashes = [database_row_revision(row) for row in rows.rows]
        content: dict[str, Any] = {
            "table": query.table,
            "rows": list(rows.rows),
            "read_at": read_at,
            "truncated": rows.truncated,
        }
        if table_schema is not None:
            content["table_schema"] = table_schema
        checksum = f"sha256:{sha256_hex(canonical_json(content))}"
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "postgres",
                **content,
                "content_hash": checksum,
                "row_hashes": row_hashes,
                "warnings": ["More rows may be available; this result is a bounded live read."]
                if rows.truncated
                else [],
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="database",
                    source_uri=(
                        f"postgres://integration/{bound.integration.integration_id}/tables/"
                        f"{quote(query.table, safe='')}"
                    ),
                    source_locator={
                        "integration_id": str(bound.integration.integration_id),
                        "table": query.table,
                        "read_at": read_at,
                        "offset": query.offset,
                        "row_count": len(rows.rows),
                        "filters": dict(query.filters),
                        "columns": list(query.columns),
                        "row_hashes": row_hashes,
                        "truncated": rows.truncated,
                    },
                    content_hash=checksum,
                    metadata={
                        "provider": "postgres",
                        "binding_checksum": bound.checksum,
                        **({"binding_id": str(context.tool.binding_id)} if modern else {}),
                    },
                ),
            ),
        )

    async def _same_bound(
        self, context: RunToolContext, bound: BoundRunResource, password: str
    ) -> None:
        """エラーや cache hit でも、現在の撤権と Secret rotation を検査する。"""
        current, current_password = await self._bound(context)
        if current != bound or current_password != password:
            raise ToolProviderError(
                "unavailable", "Database binding changed during read", retryable=False
            )

    async def _validate_recovery(self, context: RunToolContext, table: str, recovery: Any) -> None:
        """明示した関連だけを検証し、Provider は SQL や条件を変更しない。"""
        try:
            if not isinstance(recovery, Mapping) or set(recovery) != {
                "failed_tool_call_id",
                "schema_evidence_ref",
            }:
                raise ValueError("Invalid recovery reference")
            await self._observations.recovery(
                context, table, recovery["failed_tool_call_id"], recovery["schema_evidence_ref"]
            )
        except (ValueError, TypeError, KeyError):
            raise ToolProviderError(
                "invalid_request",
                "Recovery references are invalid or the single correction was already attempted",
                retryable=False,
            ) from None

    async def _describe(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """同一 read binding の構造だけを取得し、cache hit も新鮮な観測とは呼ばない。"""
        if not context.run or not runtime_policy(context.run.task_brief or {}):
            raise ToolProviderError(
                "invalid_request",
                "Schema inspection requires the frozen runtime policy",
                retryable=False,
            )
        try:
            if (
                set(arguments) - {"table", "purpose", "refresh", "recovery_from"}
                or type(arguments.get("refresh", False)) is not bool
            ):
                raise ValueError("Invalid schema request")
            query = build_database_query({"table": arguments.get("table"), "include_schema": True})
        except ValueError:
            raise ToolProviderError(
                "invalid_request", "Schema request requires one exact table", retryable=False
            ) from None
        bound, password = await self._bound(context)
        if not scope_values_allow(bound.scope.get("tables", []), query.table):
            raise ToolProviderError(
                "scope_denied", "Table is outside the frozen scope", retryable=False
            )
        recovery_from = arguments.get("recovery_from")
        if recovery_from:
            try:
                await self._observations.recovery(context, query.table, recovery_from)
            except (ValueError, TypeError):
                raise ToolProviderError(
                    "invalid_request",
                    "Schema recovery reference is invalid or already attempted",
                    retryable=False,
                ) from None
        observation = (
            None
            if arguments.get("refresh") or recovery_from
            else await self._observations.lookup(context, bound, query.table)
        )
        cached = observation is not None
        if observation is None:
            try:
                rows = await self._source.describe(bound.integration.config, password, query.table)
                schema = validate_table_schema(rows.table_schema)
            except DatabaseReadError as error:
                await self._same_bound(context, bound, password)
                d = error.diagnostic
                raise ToolProviderError(
                    d.code,
                    d.message,
                    retryable=False,
                    diagnostic={
                        "reason_code": d.reason_code,
                        "stage": d.stage,
                        "sqlstate": d.sqlstate,
                    },
                ) from None
            observation = {
                "table_schema": schema,
                "schema_checksum": "sha256:" + sha256_hex(canonical_json(schema)),
                "observed_at": datetime.now(UTC).isoformat(),
                "observation_refs": [],
            }
        await self._same_bound(context, bound, password)
        safe_observation(
            "database.schema.observed",
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            reason_code="cache_hit"
            if cached
            else "refresh"
            if arguments.get("refresh") or recovery_from
            else "source_read",
        )
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "postgres",
                "table": query.table,
                **observation,
                "cached": cached,
                "warnings": [
                    "Structure is an observation, not current DDL or write permission. Refresh "
                    "when it may have changed."
                ],
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="database_schema",
                    source_uri=(
                        f"postgres://integration/{bound.integration.integration_id}/tables/"
                        f"{quote(query.table, safe='')}"
                    ),
                    source_locator={"table": query.table, **observation, "cached": cached},
                    content_hash=observation["schema_checksum"],
                    metadata={
                        "provider": "postgres",
                        "binding_checksum": bound.checksum,
                        "binding_id": str(context.tool.binding_id),
                    },
                ),
            ),
        )

    async def project_facts(
        self, claimed: ClaimedRun, tools: Sequence[RegisteredTool], workspace: RunWorkspace
    ) -> list[dict[str, Any]]:
        """次 Segment へ確定値をそのまま投影し、モデルに schema の転記を要求しない。"""
        if not runtime_policy(claimed.task_snapshot_json) or claimed.run_segment_id is None:
            return []
        try:
            frozen, before = await self._observations.segment_index(
                claimed.run_id, claimed.run_segment_id
            )
            facts: list[dict[str, Any]] = []
            for tool in tools:
                if tool.capability != "database.describe/v1":
                    continue
                context = RunToolContext(
                    run_id=claimed.run_id,
                    run_attempt_id=claimed.run_attempt_id,
                    project_id=claimed.project_id,
                    user_id=claimed.actor_id,
                    tool=tool,
                    workspace=workspace,
                )
                bound, password = await self._bound(context)
                if frozen is not None:
                    candidates = [
                        item for item in frozen if item["binding_id"] == str(tool.binding_id)
                    ]
                    for item in candidates:
                        if item["binding_checksum"] != bound.checksum or not scope_values_allow(
                            bound.scope.get("tables", []), item["table"]
                        ):
                            raise ToolProviderError(
                                "scope_denied",
                                "Schema observation is no longer authorized",
                                retryable=False,
                            )
                    facts.extend(candidates)
                else:
                    for table in await self._observations.tables(context, before):
                        if not scope_values_allow(bound.scope.get("tables", []), table):
                            continue
                        observation = await self._observations.lookup(
                            context, bound, table, before=before
                        )
                        if observation:
                            facts.append(
                                {
                                    "table": table,
                                    "binding_id": str(tool.binding_id),
                                    "integration_id": str(tool.integration_id),
                                    "binding_checksum": bound.checksum,
                                    **observation,
                                }
                            )
                await self._same_bound(context, bound, password)
            # cache の容量は業務条件ではない。超過時は通常の describe へ戻る。
            if len(facts) > 20 or len(canonical_json(facts).encode("utf-8")) > MAX_DATABASE_BYTES:
                return []
            safe_observation(
                "database.schema.projected",
                run_id=claimed.run_id,
                run_attempt_id=claimed.run_attempt_id,
                sample_count=len(facts),
            )
            return facts
        except (SQLAlchemyError, OSError, ValueError, KeyError, TypeError):
            return []
