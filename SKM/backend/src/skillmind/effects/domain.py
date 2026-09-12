"""ChangeProposal、Approval と EffectExecution の domain 状態と read model を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ChangeProposalStatus(StrEnum):
    """Observe/propose/apply chain 上の Proposal lifecycle。"""

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    FAILED = "FAILED"


class EffectRiskLevel(StrEnum):
    """外部 effect が持つ platform 共通 risk level。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalSource(StrEnum):
    """Proposal を許可/拒否した判断源。"""

    USER = "USER"
    PREAUTHORIZATION = "PREAUTHORIZATION"


class ApprovalDecision(StrEnum):
    """Proposal version に対する一度だけの判断。"""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EffectExecutionStatus(StrEnum):
    """承認後 apply worker の durable lifecycle。"""

    REQUESTED = "REQUESTED"
    LEASED = "LEASED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    STALE = "STALE"
    FAILED = "FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class PreauthorizationStatus(StrEnum):
    """低 risk effect policy が新規 Proposal に適用可能かを表す。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ChangeProposalNotFoundError(LookupError):
    """Project/Run 境界内に Proposal が存在しないことを表す。"""


class ChangeProposalConflictError(ValueError):
    """Proposal version/checksum/status/idempotency の競合を表す。"""


class ChangeProposalExpiredError(ValueError):
    """期限切れ Proposal の approval/apply が拒否されたことを表す。"""


class ChangeProposalApprovalForbiddenError(PermissionError):
    """Actor が Run の effect approver として許可されていないことを表す。"""


class ChangeProposalValidationError(ValueError):
    """Proposal が Blueprint、binding、scope または evidence 境界に違反したことを表す。"""


class EffectPreauthorizationNotFoundError(LookupError):
    """Project 境界内に preauthorization が存在しないことを表す。"""


class EffectLeaseValidationError(RuntimeError):
    """EffectExecution lease が不正または期限切れであることを表す。"""


@dataclass(frozen=True, slots=True)
class ChangeProposalDraft:
    """Agent control Tool contract を通過した、まだ権限を持たない Proposal candidate。"""

    effect_intent_key: str
    resource_key: str
    capability_version: str
    operation: str
    target: dict[str, Any]
    changes: tuple[dict[str, Any], ...]
    precondition: dict[str, Any]
    summary: str
    evidence_refs: tuple[str, ...]
    risk_level: EffectRiskLevel
    reversible: bool
    rollback: dict[str, Any]
    verification: dict[str, Any]
    continuation_mode: str
    checkpoint: dict[str, Any]
    checkpoint_checksum: str
    idempotency_key: str
    request_fingerprint: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredChangeProposal:
    """公開可能な ChangeProposal snapshot。Secret/config は含まない。"""

    proposal_id: UUID
    proposal_ref: str
    project_id: UUID
    run_id: UUID
    run_segment_id: UUID
    agent_session_id: UUID
    target_binding_id: UUID
    integration_id: UUID | None
    effect_intent_key: str
    capability_version: str
    operation: str
    target: dict[str, Any]
    summary: str
    changes: tuple[dict[str, Any], ...]
    precondition: dict[str, Any]
    evidence_refs: tuple[str, ...]
    risk_level: EffectRiskLevel
    reversible: bool
    rollback: dict[str, Any]
    verification: dict[str, Any]
    continuation_mode: str
    checkpoint: dict[str, Any]
    checkpoint_checksum: str
    idempotency_key: str
    status: ChangeProposalStatus
    version: int
    checksum: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CreatePreauthorizationCommand:
    """ADMIN が低 risk の精確 scope に限定して作成する policy command。"""

    project_id: UUID
    integration_id: UUID
    capability_version: str
    operation: str
    scope: dict[str, Any]
    created_by: UUID
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredEffectPreauthorization:
    """公開可能な preauthorization policy snapshot。"""

    preauthorization_id: UUID
    project_id: UUID
    integration_id: UUID
    capability_version: str
    operation: str
    max_risk_level: EffectRiskLevel
    scope: dict[str, Any]
    status: PreauthorizationStatus
    policy_version: int
    created_by: UUID
    expires_at: datetime | None
    disabled_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DecideProposalCommand:
    """Proposal の version/checksum へ actor が行う idempotent 判断 command。"""

    project_id: UUID
    run_id: UUID
    proposal_id: UUID
    actor_id: UUID
    actor_is_administrator: bool
    decision: ApprovalDecision
    proposal_version: int
    proposal_checksum: str
    idempotency_key: str
    reason: str
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class StoredChangeApproval:
    """Proposal version に紐づく追加式 approval read model。"""

    approval_id: UUID
    proposal_id: UUID
    run_id: UUID
    source: ApprovalSource
    decision: ApprovalDecision
    actor_id: UUID | None
    preauthorization_id: UUID | None
    proposal_version: int
    proposal_checksum: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEffectExecution:
    """Effect apply と read-back verification の公開 read model。"""

    effect_execution_id: UUID
    proposal_id: UUID
    run_id: UUID
    approval_id: UUID
    tool_call_id: UUID | None
    status: EffectExecutionStatus
    provider: str
    provider_version: str
    before_ref: str | None
    after_ref: str | None
    verification: dict[str, Any]
    error: dict[str, Any] | None
    attempt_no: int
    executed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProposalDecisionResult:
    """判断後の Proposal/Approval/Effect または継続 Run status。"""

    proposal: StoredChangeProposal
    approval: StoredChangeApproval
    effect_execution: StoredEffectExecution | None
    run_status: str
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ClaimedEffectExecution:
    """Effect Worker が Provider 実行へ必要な immutable snapshot と lease。"""

    effect_execution_id: UUID
    proposal_id: UUID
    proposal_ref: str
    approval_id: UUID
    run_id: UUID
    run_segment_id: UUID
    run_attempt_id: UUID
    agent_session_id: UUID
    project_id: UUID
    integration_id: UUID | None
    binding_id: UUID
    capability_version: str
    operation: str
    target: dict[str, Any]
    changes: tuple[dict[str, Any], ...]
    precondition: dict[str, Any]
    verification: dict[str, Any]
    idempotency_key: str
    request_fingerprint: str
    provider: str
    integration_revision: int
    integration_scope: dict[str, Any]
    integration_config: dict[str, Any]
    secret_reference_id: UUID | None
    lease_token: str
    lease_expires_at: datetime
    attempt_no: int


@dataclass(frozen=True, slots=True)
class EffectStepAuthority:
    """共有段階認可の lock 下で確認した発起人。transaction 外の許可証としては使わない。"""

    organization_id: UUID
    actor_id: UUID


@dataclass(frozen=True, slots=True)
class EffectEvidenceDraft:
    """Provider observe/apply/read-back が返す Evidence の保存前表現。"""

    evidence_type: str
    source_uri: str
    source_locator: dict[str, Any]
    content: dict[str, Any]
    excerpt: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EffectProviderResult:
    """Provider apply 後の before/after facts と検証結果。"""

    before: EffectEvidenceDraft
    after: EffectEvidenceDraft
    verification: dict[str, Any]
    replayed: bool


@dataclass(frozen=True, slots=True)
class EffectFailure:
    """Provider 前後で分類済みの安全な effect failure。"""

    status: EffectExecutionStatus
    code: str
    retryable: bool
