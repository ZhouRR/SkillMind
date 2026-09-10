"""成功した原 Tool と不可変 Evidence byte だけを読む Artifact repository。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.artifacts.domain import (
    MAX_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACT_BYTES,
    MAX_RUN_ARTIFACTS,
    ArtifactContent,
    ArtifactIntegrityError,
    ArtifactMetadata,
)
from skillmind.db.models import Evidence, Run, RunAttempt, ToolCall


class ArtifactRepository:
    """呼出元の認可済み Project/Run 内で、歴史を修復せず同じ保存行を照合する。"""

    def __init__(self, session: AsyncSession) -> None:
        """API 読取と checkpoint transaction で再利用する session を保持する。"""

        self._session = session

    async def list_metadata(
        self, *, project_id: UUID, run_id: UUID,
    ) -> tuple[ArtifactMetadata, ...]:
        """本文は取得せず最大 100 件/10 MiB の公開 metadata と原 Tool の整合を確認する。"""

        statement = (
            _statement(content=False)
            .where(Run.project_id == project_id, Evidence.run_id == run_id, _has_binding())
            .order_by(Evidence.created_at, Evidence.id)
            .limit(MAX_RUN_ARTIFACTS + 1)
        )
        rows = (await self._session.execute(statement)).mappings().all()
        result = tuple(_metadata(row, run_id=run_id, project_id=project_id) for row in rows)
        _validate_total(result)
        return result

    async def get_content(
        self, *, project_id: UUID, run_id: UUID, artifact_ref: str,
    ) -> ArtifactContent | None:
        """DB でも limit+1 byte までを一度だけ取得し、実 size/hash/UTF-8 を確認して返す。"""

        statement = _statement(content=True).where(
            Run.project_id == project_id, Evidence.run_id == run_id,
            Evidence.artifact_ref == artifact_ref,
        ).limit(2)
        rows = (await self._session.execute(statement)).mappings().all()
        if not rows:
            return None
        if len(rows) != 1:
            raise _invalid()
        metadata = _metadata(rows[0], run_id=run_id, project_id=project_id)
        if metadata.artifact_ref != artifact_ref:
            raise _invalid()
        return ArtifactContent(metadata=metadata, content=rows[0]["content"])

    async def verified_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """Result/checkpoint が共有する実 byte 核験。欠落は集合から除き、損傷は明示拒否する。"""

        if not refs:
            return frozenset()
        if len(refs) > MAX_RUN_ARTIFACTS:
            raise _invalid()
        statement = _statement(content=True).where(
            Evidence.run_id == run_id, Evidence.artifact_ref.in_(refs),
        ).order_by(Evidence.id).limit(MAX_RUN_ARTIFACTS + 1)
        rows = (await self._session.execute(statement)).mappings().all()
        metadata: list[ArtifactMetadata] = []
        for row in rows:
            item = _metadata(row, run_id=run_id)
            if item.artifact_ref not in refs:
                raise _invalid()
            ArtifactContent(metadata=item, content=row["content"])
            metadata.append(item)
        _validate_total(tuple(metadata))
        return frozenset(item.artifact_ref for item in metadata)


def _has_binding() -> Any:
    """半 binding も一覧の検証対象にし、ref が NULL だからと損傷を隠さない。"""

    return or_(
        Evidence.artifact_ref.is_not(None), Evidence.artifact_bytes.is_not(None),
        Evidence.artifact_size.is_not(None), Evidence.artifact_mime_type.is_not(None),
        Evidence.artifact_path.is_not(None),
    )


def _statement(*, content: bool) -> Select[Any]:
    """壊れた Tool/Attempt 関連を WHERE で消さず、同一 snapshot の列だけで判定する。"""

    columns = [
        Evidence.artifact_ref, Run.project_id, Evidence.run_id, Evidence.tool_call_id,
        Evidence.evidence_ref, Evidence.artifact_path, Evidence.artifact_size,
        Evidence.artifact_mime_type, Evidence.content_hash, Evidence.created_at,
        Evidence.evidence_type, Evidence.source_uri, Evidence.source_locator,
        Evidence.snapshot_uri, Evidence.metadata_json,
        Evidence.artifact_bytes.is_not(None).label("has_content"),
        func.octet_length(Evidence.artifact_bytes).label("content_size"),
        ToolCall.id.label("tool_identity"), ToolCall.run_id.label("tool_run_id"),
        ToolCall.capability_version, ToolCall.provider, ToolCall.status, ToolCall.result_json,
        ToolCall.tool_name, ToolCall.integration_id,
        RunAttempt.run_id.label("attempt_run_id"),
    ]
    if content:
        # 壊れた BYTEA が巨大でも全体を client へ運ばず、超限の証拠となる 1 byte だけ追加する。
        columns.append(
            func.substring(Evidence.artifact_bytes, 1, MAX_ARTIFACT_BYTES + 1).label("content")
        )
    return (
        select(*columns).select_from(Evidence)
        .join(Run, Run.id == Evidence.run_id)
        .outerjoin(ToolCall, ToolCall.id == Evidence.tool_call_id)
        .outerjoin(RunAttempt, RunAttempt.id == ToolCall.run_attempt_id)
    )


def _metadata(
    row: RowMapping, *, run_id: UUID, project_id: UUID | None = None,
) -> ArtifactMetadata:
    """原成功 response の一つの Artifact/Evidence と保存行を完全に対応付ける。"""

    value = ArtifactMetadata(
        artifact_ref=row["artifact_ref"], project_id=row["project_id"], run_id=row["run_id"],
        tool_call_id=row["tool_call_id"], evidence_ref=row["evidence_ref"],
        path=row["artifact_path"], size_bytes=row["artifact_size"],
        mime_type=row["artifact_mime_type"], checksum=row["content_hash"],
        created_at=row["created_at"],
    )
    response = row["result_json"]
    locator, metadata = row["source_locator"], row["metadata_json"]
    if (
        value.run_id != run_id or (project_id is not None and value.project_id != project_id)
        or row["tool_identity"] != value.tool_call_id or row["tool_run_id"] != value.run_id
        or row["attempt_run_id"] != value.run_id or row["status"] != "SUCCEEDED"
        or row["capability_version"] != "workspace.write/v2" or row["provider"] != "workspace"
        or row["tool_name"] != "mcp__skillmind__workspace_write_v2"
        or row["integration_id"] is not None or row["evidence_type"] != "workspace-write"
        or row["source_uri"] != f"workspace://runs/{run_id}/{quote(value.path, safe='/')}"
        or row["snapshot_uri"] is not None
        or not isinstance(locator, dict) or type(locator.get("bytes")) is not int
        or locator != {"path": value.path, "bytes": value.size_bytes}
        or not isinstance(metadata, dict) or metadata.get("read_only") is not False
        or metadata != {"scope": "run-workspace", "read_only": False}
        or row["has_content"] is not True or type(row["content_size"]) is not int
        or row["content_size"] != value.size_bytes or not isinstance(response, dict)
        or response.get("status") != "success" or response.get("provider") != "workspace"
        or response.get("path") != value.path or response.get("content_hash") != value.checksum
        or type(response.get("bytes_written")) is not int
        or response["bytes_written"] != value.size_bytes
        or response.get("artifact_refs") != [value.artifact_ref]
        or response.get("evidence_refs") != [value.evidence_ref]
    ):
        raise _invalid()
    return value


def _validate_total(values: tuple[ArtifactMetadata, ...]) -> None:
    """重複・超限を切り捨てた正常一覧に変えず、保存資産の総量違反として拒否する。"""

    if (
        len(values) > MAX_RUN_ARTIFACTS
        or len({value.artifact_ref for value in values}) != len(values)
        or sum(value.size_bytes for value in values) > MAX_RUN_ARTIFACT_BYTES
    ):
        raise _invalid()


def _invalid() -> ArtifactIntegrityError:
    """内部 identity や原 response を HTTP/監査 error に混入させない。"""

    return ArtifactIntegrityError("Artifact does not match its saved identity or content")
