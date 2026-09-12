"""原 Run の批准・適用・Tool 成功・Evidence を結合し、文書取得の前置事実を読む。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from skillmind.db.models import (
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    ResourceBinding,
    Run,
    ToolCall,
)
from skillmind.documents.snapshot import DOCUMENT_CAPABILITIES
from skillmind.skills.document_prerequisites import document_prerequisites
from skillmind.skills.frozen_manifest import verified_run_manifest


@dataclass(frozen=True, slots=True)
class DocumentReadiness:
    """モデルが作成できない、正本の前置条件と確認済み intent の投影。"""

    required: tuple[str, ...]
    applied: tuple[str, ...]

    @property
    def ready(self) -> bool:
        """要求されたすべての効果が確定した場合だけ取得可能とする。"""
        return self.required == self.applied


async def load_document_readiness(session: AsyncSession, run: Run) -> DocumentReadiness:
    """外部 DB を再照会せず、元の書込回执と同じ transaction で確定した事実を読む。"""

    manifest = verified_run_manifest(
        run.task_snapshot_json.get("skill_snapshots", ()), run.task_snapshot_json
    )
    required = document_prerequisites(manifest, str(run.task_snapshot_json.get("task_key", "")))
    applied: list[str] = []
    before, after = aliased(Evidence), aliased(Evidence)
    for prerequisite in required:
        # Run lock は呼出側 audit が所有する。履歴の別 row を lock して順序を逆転しない。
        # JSON/JSONB の両方で空値を扱い、JSON 同士の等値演算には依存しない。
        statement = (
            select(EffectExecution.id)
            .join(ChangeProposal, ChangeProposal.id == EffectExecution.proposal_id)
            .join(ChangeApproval, ChangeApproval.id == EffectExecution.approval_id)
            .join(ResourceBinding, ResourceBinding.id == ChangeProposal.target_binding_id)
            .join(ToolCall, ToolCall.id == EffectExecution.tool_call_id)
            .join(
                before,
                (before.evidence_ref == EffectExecution.before_ref)
                & (before.run_id == run.id)
                & (before.tool_call_id == ToolCall.id),
            )
            .join(
                after,
                (after.evidence_ref == EffectExecution.after_ref)
                & (after.run_id == run.id)
                & (after.tool_call_id == ToolCall.id),
            )
            .where(
                EffectExecution.run_id == run.id,
                EffectExecution.status == "APPLIED",
                or_(
                    EffectExecution.error_json.is_(None),
                    cast(EffectExecution.error_json, String) == "null",
                ),
                EffectExecution.executed_at.is_not(None),
                ChangeProposal.run_id == run.id,
                ChangeProposal.project_id == run.project_id,
                ChangeProposal.status == "APPLIED",
                ChangeProposal.effect_intent_key == prerequisite.intent_key,
                ChangeProposal.operation == prerequisite.operation,
                ChangeApproval.run_id == run.id,
                ChangeApproval.proposal_id == ChangeProposal.id,
                ChangeApproval.decision == "APPROVED",
                ChangeApproval.proposal_version == ChangeProposal.version,
                ChangeApproval.proposal_checksum == ChangeProposal.checksum,
                ResourceBinding.run_id == run.id,
                ResourceBinding.project_id == run.project_id,
                ResourceBinding.scope_level == "RUN",
                ResourceBinding.requirement_key == prerequisite.resource_key,
                ResourceBinding.provider == EffectExecution.provider,
                ResourceBinding.integration_id.is_not_distinct_from(ChangeProposal.integration_id),
                ToolCall.integration_id.is_not_distinct_from(ChangeProposal.integration_id),
                ToolCall.run_id == run.id,
                ToolCall.status == "SUCCEEDED",
                or_(ToolCall.error_json.is_(None), cast(ToolCall.error_json, String) == "null"),
                ToolCall.result_json.is_not(None),
                cast(ToolCall.result_json, String) != "null",
                ToolCall.capability_version == ChangeProposal.capability_version,
                ToolCall.provider == EffectExecution.provider,
                ToolCall.result_json["status"].as_string() == "success",
                ToolCall.result_json["provider"].as_string() == EffectExecution.provider,
                ToolCall.result_json["proposal_ref"].as_string() == ChangeProposal.proposal_ref,
                ToolCall.result_json["before_ref"].as_string() == EffectExecution.before_ref,
                ToolCall.result_json["after_ref"].as_string() == EffectExecution.after_ref,
                before.evidence_ref != after.evidence_ref,
            )
            .limit(1)
        )
        if await session.scalar(statement) is not None:
            applied.append(prerequisite.intent_key)
    return DocumentReadiness(tuple(item.intent_key for item in required), tuple(applied))


async def require_document_readiness(session: AsyncSession, run: Run, capability: str) -> None:
    """文書の全取得 Tool に同じ gate を適用し、批准だけや checkpoint 文言を受理しない。"""

    if capability not in DOCUMENT_CAPABILITIES:
        return
    state = await load_document_readiness(session, run)
    if not state.ready:
        raise PermissionError("Required controlled effects have not been confirmed")
