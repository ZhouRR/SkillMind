"""Proposal・批准・Effect の公開投影を読取と状態更新の応答で共有する。"""

from __future__ import annotations

from skillmind.db.models import ChangeApproval, ChangeProposal, EffectExecution
from skillmind.effects.domain import (
    ApprovalDecision,
    ApprovalSource,
    ChangeProposalStatus,
    EffectExecutionStatus,
    EffectRiskLevel,
    StoredChangeApproval,
    StoredChangeProposal,
    StoredEffectExecution,
)


def stored_change_proposal(row: ChangeProposal) -> StoredChangeProposal:
    """ChangeProposal ORM row を公開 read model へ変換する。"""

    changes = row.preview_json.get("changes", [])
    return StoredChangeProposal(
        proposal_id=row.id,
        proposal_ref=row.proposal_ref,
        project_id=row.project_id,
        run_id=row.run_id,
        run_segment_id=row.run_segment_id,
        agent_session_id=row.agent_session_id,
        target_binding_id=row.target_binding_id,
        integration_id=row.integration_id,
        effect_intent_key=row.effect_intent_key,
        capability_version=row.capability_version,
        operation=row.operation,
        target=dict(row.target_json),
        summary=row.summary,
        changes=(
            tuple(dict(item) for item in changes if isinstance(item, dict))
            if isinstance(changes, list)
            else ()
        ),
        precondition=dict(row.precondition_json),
        evidence_refs=tuple(row.evidence_refs_json),
        risk_level=EffectRiskLevel(row.risk_level),
        reversible=row.reversible,
        rollback=dict(row.rollback_json),
        verification=dict(row.verification_json),
        continuation_mode=row.continuation_mode,
        checkpoint=dict(row.checkpoint_json),
        checkpoint_checksum=row.checkpoint_checksum,
        idempotency_key=row.idempotency_key,
        status=ChangeProposalStatus(row.status),
        version=row.version,
        checksum=row.checksum,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def stored_change_approval(row: ChangeApproval) -> StoredChangeApproval:
    """ChangeApproval ORM row を公開 read model へ変換する。"""

    return StoredChangeApproval(
        approval_id=row.id,
        proposal_id=row.proposal_id,
        run_id=row.run_id,
        source=ApprovalSource(row.source),
        decision=ApprovalDecision(row.decision),
        actor_id=row.actor_id,
        preauthorization_id=row.preauthorization_id,
        proposal_version=row.proposal_version,
        proposal_checksum=row.proposal_checksum,
        reason=row.reason,
        created_at=row.created_at,
    )


def stored_effect_execution(row: EffectExecution) -> StoredEffectExecution:
    """EffectExecution ORM row を lease/credential なしの公開 read model へ変換する。"""

    return StoredEffectExecution(
        effect_execution_id=row.id,
        proposal_id=row.proposal_id,
        run_id=row.run_id,
        approval_id=row.approval_id,
        tool_call_id=row.tool_call_id,
        status=EffectExecutionStatus(row.status),
        provider=row.provider,
        provider_version=row.provider_version,
        before_ref=row.before_ref,
        after_ref=row.after_ref,
        verification=dict(row.verification_json),
        error=dict(row.error_json) if row.error_json is not None else None,
        attempt_no=row.attempt_no,
        executed_at=row.executed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
