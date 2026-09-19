"""Run 所有の監査を明示選択で読む。Provider 再実行とモデルによる欠落補完を行わない。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.tool_gateway import RunToolContext
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    Run,
    ToolCall,
    User,
)
from skillmind.projects.domain import ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository


class PostgresAuditExportSource:
    """既存 Project 読取規則と Run 帰属を、短い session 内で確認する read port。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Worker と同じ監査 DB の session factory を保持する。"""
        self._sessions = sessions

    async def _authorize(self, session: AsyncSession, context: RunToolContext) -> None:
        """別 Project、失効 user、撤回された所属を同じ不在境界へ閉じる。"""
        user = await session.get(User, context.user_id, populate_existing=True)
        run = await session.get(Run, context.run_id, populate_existing=True)
        if (
            user is None
            or user.status != "ACTIVE"
            or run is None
            or run.project_id != context.project_id
            or run.permission_snapshot_json.get("actor_id") != str(context.user_id)
        ):
            raise PermissionError("Audit unavailable")
        # 共通の現在 Project/所属チェックを再利用する。外部資格情報は解決しない。
        repository = ProjectRepository(session)
        try:
            locked = await repository.lock_write_access(user=user, project_id=context.project_id)
            repository.require_read_access(locked)
        except ProjectNotFoundError:
            raise PermissionError("Audit unavailable") from None

    async def authorize(self, context: RunToolContext) -> None:
        """I/O 前後の現在の参照権限を独立 transaction で再確認する。"""
        async with self._sessions() as session, session.begin():
            await self._authorize(session, context)

    async def read(
        self,
        context: RunToolContext,
        *,
        evidence_refs: tuple[str, ...],
        proposal_refs: tuple[str, ...],
    ) -> dict[str, Any]:
        """集合 SELECT で保存済み値を得る。秘密/config/Artifact 本文は query へ含めない。"""
        async with self._sessions() as session, session.begin():
            await self._authorize(session, context)
            evidence_rows = (
                (
                    await session.execute(
                        select(Evidence, ToolCall)
                        .join(ToolCall, ToolCall.id == Evidence.tool_call_id)
                        .where(
                            Evidence.run_id == context.run_id,
                            ToolCall.run_id == context.run_id,
                            Evidence.evidence_ref.in_(evidence_refs),
                        )
                    )
                ).all()
                if evidence_refs
                else []
            )
            evidence = {row.evidence_ref: _evidence(row, call) for row, call in evidence_rows}
            proposals = (
                list(
                    await session.scalars(
                        select(ChangeProposal).where(
                            ChangeProposal.run_id == context.run_id,
                            ChangeProposal.project_id == context.project_id,
                            ChangeProposal.proposal_ref.in_(proposal_refs),
                        )
                    )
                )
                if proposal_refs
                else []
            )
            if set(evidence) != set(evidence_refs) or {p.proposal_ref for p in proposals} != set(
                proposal_refs
            ):
                raise LookupError("Audit selection unavailable")
            proposal_ids = [p.id for p in proposals]
            approvals = (
                list(
                    await session.scalars(
                        select(ChangeApproval)
                        .where(
                            ChangeApproval.run_id == context.run_id,
                            ChangeApproval.proposal_id.in_(proposal_ids),
                        )
                        .order_by(ChangeApproval.created_at, ChangeApproval.id)
                    )
                )
                if proposal_ids
                else []
            )
            effects = (
                list(
                    await session.scalars(
                        select(EffectExecution)
                        .where(
                            EffectExecution.run_id == context.run_id,
                            EffectExecution.proposal_id.in_(proposal_ids),
                        )
                        .order_by(EffectExecution.id)
                    )
                )
                if proposal_ids
                else []
            )
            projected = {
                p.proposal_ref: {
                    "proposal_ref": p.proposal_ref,
                    **_fields(
                        p,
                        (
                            "id",
                            "capability_version",
                            "operation",
                            "status",
                            "target_json",
                            "preview_json",
                            "precondition_json",
                            "verification_json",
                            "evidence_refs_json",
                            "checksum",
                            "created_at",
                            "updated_at",
                        ),
                    ),
                    "approvals": [
                        _fields(a, ("id", "source", "decision", "proposal_checksum", "created_at"))
                        for a in approvals
                        if a.proposal_id == p.id
                    ],
                    "effects": [
                        _fields(
                            e,
                            (
                                "id",
                                "status",
                                "attempt_no",
                                "before_ref",
                                "after_ref",
                                "verification_json",
                                "error_json",
                                "executed_at",
                                "updated_at",
                            ),
                        )
                        for e in effects
                        if e.proposal_id == p.id
                    ],
                }
                for p in proposals
            }
            return {
                "evidence": [evidence[ref] for ref in evidence_refs],
                "proposals": [projected[ref] for ref in proposal_refs],
            }


def _fields(row: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """許可列だけを defensive copy する。ORM 全体の自動 serialization はしない。"""
    return {name: _json_value(getattr(row, name)) for name in names}


def _json_value(value: Any) -> Any:
    """JSON にない識別子と時刻だけ変換し、観測した文字列・改行はそのまま保持する。"""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return deepcopy(value)


def _evidence(row: Evidence, call: ToolCall) -> dict[str, Any]:
    """snapshot/応答/抜粋の違いを維持し、保存していない原引数を捏造しない。"""
    if call.status not in {"SUCCEEDED", "FAILED", "DENIED"}:
        raise ValueError("Audit invocation is unfinished")
    metadata: Mapping[str, Any] = row.metadata_json
    if "snapshot" in metadata and row.content_hash != "sha256:" + sha256_hex(
        canonical_json(metadata["snapshot"])
    ):
        raise ValueError("Audit snapshot integrity failed")
    return {
        **_fields(
            row,
            (
                "evidence_ref",
                "evidence_type",
                "source_uri",
                "source_locator",
                "content_hash",
                "snapshot_uri",
                "excerpt",
                "metadata_json",
                "artifact_ref",
                "created_at",
            ),
        ),
        "tool_call": _fields(
            call,
            (
                "id",
                "tool_name",
                "capability_version",
                "provider",
                "status",
                "arguments_summary",
                "request_fingerprint",
                "result_json",
                "error_json",
                "duration_ms",
                "created_at",
                "updated_at",
            ),
        ),
        "request_content": (
            "Only the stored arguments summary is available for this ToolCall; "
            "exact effect payloads remain in proposal records."
        ),
    }
