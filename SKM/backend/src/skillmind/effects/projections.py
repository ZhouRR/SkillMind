"""Proposal・批准・Effect の記録組立と公開投影を共有する。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import ChangeApproval, ChangeProposal, EffectExecution, Evidence
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.domain import (
    ApprovalDecision,
    ApprovalSource,
    ChangeProposalStatus,
    ChangeProposalValidationError,
    EffectEvidenceDraft,
    EffectExecutionStatus,
    EffectRiskLevel,
    StoredChangeApproval,
    StoredChangeProposal,
    StoredEffectExecution,
)
from skillmind.effects.mcp_diagnostics import diagnostic_message
from skillmind.effects.outcomes import UNKNOWN_EFFECT_CODE
from skillmind.effects.postgres_native import SQL_FAILURE_MESSAGES, sql_failure_message


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


def effect_evidence_row(
    *,
    run_id: UUID,
    tool_call_id: UUID,
    evidence_ref: str,
    draft: EffectEvidenceDraft,
    now: datetime,
) -> Evidence:
    """Provider draft を content hash と scoped snapshot metadata 付き Evidence へ変換する。"""

    content = dict(draft.content)
    return Evidence(
        id=uuid4(),
        evidence_ref=evidence_ref,
        run_id=run_id,
        tool_call_id=tool_call_id,
        evidence_type=draft.evidence_type,
        source_uri=draft.source_uri,
        source_locator=dict(draft.source_locator),
        content_hash=f"sha256:{sha256_hex(canonical_json(content))}",
        snapshot_uri=None,
        excerpt=draft.excerpt,
        metadata_json={**dict(draft.metadata), "snapshot": content},
        created_at=now,
    )


def new_effect_execution(
    *,
    proposal: ChangeProposal,
    approval: ChangeApproval,
    provider: str,
    now: datetime,
) -> EffectExecution:
    """Approved Proposal から一意な REQUESTED EffectExecution を作成する。"""

    capability = resolve_effect_capability(proposal.capability_version)
    if provider not in capability.providers:
        raise ChangeProposalValidationError("Effect Provider is not registered")
    return EffectExecution(
        id=uuid4(),
        proposal_id=proposal.id,
        run_id=proposal.run_id,
        approval_id=approval.id,
        tool_call_id=None,
        status=EffectExecutionStatus.REQUESTED.value,
        provider=provider,
        provider_version=capability.provider_versions[provider],
        idempotency_key=proposal.idempotency_key,
        request_fingerprint=proposal.request_fingerprint,
        before_ref=None,
        after_ref=None,
        verification_json={},
        error_json=None,
        worker_id=None,
        lease_token_hash=None,
        lease_expires_at=None,
        heartbeat_at=None,
        attempt_no=0,
        started_at=None,
        executed_at=None,
        created_at=now,
        updated_at=now,
    )


def effect_tool_error(
    *, code: str, retryable: bool, diagnostic: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tool contract に適合し、外部 response 本文を含まない effect error を返す。"""

    public_code = code
    if code in SQL_FAILURE_MESSAGES:
        public_code = {
            "sql_request_not_sent": "unavailable",
            "sql_permission_denied": "scope_denied",
            "sql_undefined_table": "not_found",
        }.get(code, "invalid_request")
    elif code in {"mcp_request_not_sent", "http_response_error"}:
        public_code = "invalid_request"
    return {
        "status": "error",
        "code": public_code,
        "message": (
            "Original external write result requires reconciliation"
            if code == UNKNOWN_EFFECT_CODE
            else sql_failure_message(code)
            if code in SQL_FAILURE_MESSAGES
            else "Controlled effect could not be completed"
        )
        + diagnostic_message(diagnostic)
        + (" Original HTTP response: " + canonical_json(response) if response is not None else ""),
        "retryable": retryable,
    }
