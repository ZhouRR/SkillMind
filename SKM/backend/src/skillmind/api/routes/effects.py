"""ChangeProposal decision と ADMIN preauthorization policy API を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field

from skillmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    ProjectWriteActor,
    authorize_project_access,
)
from skillmind.api.problems import ProblemException
from skillmind.effects import (
    ApprovalDecision,
    ApprovalSource,
    ChangeProposalApprovalForbiddenError,
    ChangeProposalConflictError,
    ChangeProposalExpiredError,
    ChangeProposalNotFoundError,
    ChangeProposalStatus,
    ChangeProposalValidationError,
    CreatePreauthorizationCommand,
    DecideProposalCommand,
    EffectExecutionStatus,
    EffectPreauthorizationNotFoundError,
    EffectRiskLevel,
    PreauthorizationStatus,
    ProposalDecisionResult,
    StoredChangeApproval,
    StoredChangeProposal,
    StoredEffectExecution,
    StoredEffectPreauthorization,
)
from skillmind.effects.service import EffectService
from skillmind.integrations import (
    IntegrationNotFoundError,
    IntegrationValidationError,
    ensure_explicit_scope,
)
from skillmind.runs.service import RunService

router = APIRouter()


class ChangeProposalResponse(BaseModel):
    """Secret/config/request fingerprint を除外した ChangeProposal response。"""

    proposal_id: UUID
    proposal_ref: str
    project_id: UUID
    run_id: UUID
    run_segment_id: UUID
    agent_session_id: UUID
    target_binding_id: UUID
    integration_id: UUID
    effect_intent_key: str
    capability_version: str
    operation: str
    target: dict[str, Any]
    summary: str
    changes: list[dict[str, Any]]
    precondition: dict[str, Any]
    evidence_refs: list[str]
    risk_level: EffectRiskLevel
    reversible: bool
    rollback: dict[str, Any]
    verification: dict[str, Any]
    status: ChangeProposalStatus
    version: int
    checksum: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ChangeApprovalResponse(BaseModel):
    """Proposal version と判断源を公開する approval response。"""

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


class EffectExecutionResponse(BaseModel):
    """Lease/credential を除外した EffectExecution response。"""

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


class DecideProposalRequest(BaseModel):
    """Proposal version/checksum に対する approve/reject request。"""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    proposal_version: int = Field(ge=1)
    proposal_checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=1000)


class ProposalDecisionResponse(BaseModel):
    """Decision 後の Proposal、Approval と任意 EffectExecution。"""

    proposal: ChangeProposalResponse
    approval: ChangeApprovalResponse
    effect_execution: EffectExecutionResponse | None
    run_status: str
    idempotent_replay: bool


class CreatePreauthorizationRequest(BaseModel):
    """ADMIN が exact Integration/capability/operation/scope を指定する request。"""

    model_config = ConfigDict(extra="forbid")

    integration_id: UUID
    capability_version: str = Field(
        pattern=r"^[a-z][a-z0-9_.-]*/[vV][0-9][a-zA-Z0-9_.-]*$",
        max_length=128,
    )
    operation: str = Field(min_length=1, max_length=128)
    risk_level: EffectRiskLevel
    scope: dict[str, Any]
    expires_at: datetime | None = None


class DisablePreauthorizationRequest(BaseModel):
    """Optimistic policy version 付き disable request。"""

    model_config = ConfigDict(extra="forbid")

    expected_policy_version: int = Field(ge=1)


class PreauthorizationResponse(BaseModel):
    """Exact effect policy の公開 response。"""

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


class PreauthorizationListResponse(BaseModel):
    """Project の preauthorization 一覧。"""

    items: list[PreauthorizationResponse]


@router.post(
    "/projects/{project_id}/runs/{run_id}/proposals/{proposal_id}/decision",
    response_model=ProposalDecisionResponse,
    tags=["effects"],
)
async def decide_change_proposal(
    request: Request,
    project_id: UUID,
    run_id: UUID,
    proposal_id: UUID,
    actor: ProjectWriteActor,
    body: DecideProposalRequest,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ],
) -> ProposalDecisionResponse:
    """Project actor が表示中の exact Proposal version を批准または拒否する。"""

    service: RunService = request.app.state.run_service
    try:
        result = await service.decide_change_proposal(
            DecideProposalCommand(
                project_id=project_id,
                run_id=run_id,
                proposal_id=proposal_id,
                actor_id=actor.user_id,
                actor_is_administrator=actor.system_role == "ADMIN",
                decision=body.decision,
                proposal_version=body.proposal_version,
                proposal_checksum=body.proposal_checksum,
                idempotency_key=idempotency_key,
                reason=body.reason,
                trace_id=getattr(request.state, "request_id", None),
            )
        )
    except ChangeProposalNotFoundError as error:
        raise _proposal_not_found(error) from error
    except ChangeProposalExpiredError as error:
        raise _proposal_expired(error) from error
    except ChangeProposalApprovalForbiddenError as error:
        raise _proposal_approval_forbidden(error) from error
    except ChangeProposalConflictError as error:
        raise _proposal_conflict(error) from error
    except ChangeProposalValidationError as error:
        raise _proposal_rejected(error) from error
    return decision_response(result)


@router.post(
    "/projects/{project_id}/effect-preauthorizations",
    response_model=PreauthorizationResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["effects"],
)
async def create_preauthorization(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: CreatePreauthorizationRequest,
) -> PreauthorizationResponse:
    """ADMIN が LOW risk の exact scope だけを事前許可する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    if body.risk_level is not EffectRiskLevel.LOW:
        raise _proposal_rejected(ValueError("Only LOW risk may be preauthorized"))
    try:
        # 無人 apply の境界なので、Integration 側の wildcard に関わらず policy は逐項列挙とする。
        ensure_explicit_scope(body.scope)
    except IntegrationValidationError as error:
        raise _proposal_rejected(error) from error
    service: EffectService = request.app.state.effect_service
    try:
        stored = await service.create_preauthorization(
            CreatePreauthorizationCommand(
                project_id=project_id,
                integration_id=body.integration_id,
                capability_version=body.capability_version,
                operation=body.operation,
                scope=body.scope,
                created_by=actor.user_id,
                expires_at=body.expires_at,
            )
        )
    except IntegrationNotFoundError as error:
        raise _proposal_not_found(error) from error
    except IntegrationValidationError as error:
        raise _proposal_rejected(error) from error
    return _preauthorization_response(stored)


@router.get(
    "/projects/{project_id}/effect-preauthorizations",
    response_model=PreauthorizationListResponse,
    tags=["effects"],
)
async def list_preauthorizations(
    request: Request,
    project_id: UUID,
    actor: AdminReadActor,
) -> PreauthorizationListResponse:
    """ADMIN に Project の effect policies を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: EffectService = request.app.state.effect_service
    items = await service.list_preauthorizations(project_id=project_id)
    return PreauthorizationListResponse(
        items=[_preauthorization_response(item) for item in items]
    )


@router.post(
    "/projects/{project_id}/effect-preauthorizations/{preauthorization_id}/disable",
    response_model=PreauthorizationResponse,
    tags=["effects"],
)
async def disable_preauthorization(
    request: Request,
    project_id: UUID,
    preauthorization_id: UUID,
    actor: AdminWriteActor,
    body: DisablePreauthorizationRequest,
) -> PreauthorizationResponse:
    """ADMIN が policy を物理削除せず無効化する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: EffectService = request.app.state.effect_service
    try:
        stored = await service.disable_preauthorization(
            project_id=project_id,
            preauthorization_id=preauthorization_id,
            expected_policy_version=body.expected_policy_version,
        )
    except EffectPreauthorizationNotFoundError as error:
        raise _proposal_not_found(error) from error
    except ValueError as error:
        raise _proposal_conflict(error) from error
    return _preauthorization_response(stored)


def proposal_response(value: StoredChangeProposal) -> ChangeProposalResponse:
    """Proposal read model を公開 allowlist response へ変換する。"""

    return ChangeProposalResponse(
        proposal_id=value.proposal_id,
        proposal_ref=value.proposal_ref,
        project_id=value.project_id,
        run_id=value.run_id,
        run_segment_id=value.run_segment_id,
        agent_session_id=value.agent_session_id,
        target_binding_id=value.target_binding_id,
        integration_id=value.integration_id,
        effect_intent_key=value.effect_intent_key,
        capability_version=value.capability_version,
        operation=value.operation,
        target=value.target,
        summary=value.summary,
        changes=list(value.changes),
        precondition=value.precondition,
        evidence_refs=list(value.evidence_refs),
        risk_level=value.risk_level,
        reversible=value.reversible,
        rollback=value.rollback,
        verification=value.verification,
        status=value.status,
        version=value.version,
        checksum=value.checksum,
        expires_at=value.expires_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def approval_response(value: StoredChangeApproval) -> ChangeApprovalResponse:
    """Approval read model を公開 response へ変換する。"""

    return ChangeApprovalResponse(
        approval_id=value.approval_id,
        proposal_id=value.proposal_id,
        run_id=value.run_id,
        source=value.source,
        decision=value.decision,
        actor_id=value.actor_id,
        preauthorization_id=value.preauthorization_id,
        proposal_version=value.proposal_version,
        proposal_checksum=value.proposal_checksum,
        reason=value.reason,
        created_at=value.created_at,
    )


def effect_execution_response(value: StoredEffectExecution) -> EffectExecutionResponse:
    """EffectExecution read model を公開 response へ変換する。"""

    return EffectExecutionResponse(
        effect_execution_id=value.effect_execution_id,
        proposal_id=value.proposal_id,
        run_id=value.run_id,
        approval_id=value.approval_id,
        tool_call_id=value.tool_call_id,
        status=value.status,
        provider=value.provider,
        provider_version=value.provider_version,
        before_ref=value.before_ref,
        after_ref=value.after_ref,
        verification=value.verification,
        error=value.error,
        attempt_no=value.attempt_no,
        executed_at=value.executed_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def decision_response(value: ProposalDecisionResult) -> ProposalDecisionResponse:
    """Proposal decision aggregate を公開 response へ変換する。"""

    return ProposalDecisionResponse(
        proposal=proposal_response(value.proposal),
        approval=approval_response(value.approval),
        effect_execution=(
            effect_execution_response(value.effect_execution)
            if value.effect_execution is not None
            else None
        ),
        run_status=value.run_status,
        idempotent_replay=value.idempotent_replay,
    )


def _preauthorization_response(
    value: StoredEffectPreauthorization,
) -> PreauthorizationResponse:
    """Preauthorization read model を公開 response へ変換する。"""

    return PreauthorizationResponse(
        preauthorization_id=value.preauthorization_id,
        project_id=value.project_id,
        integration_id=value.integration_id,
        capability_version=value.capability_version,
        operation=value.operation,
        max_risk_level=value.max_risk_level,
        scope=value.scope,
        status=value.status,
        policy_version=value.policy_version,
        created_by=value.created_by,
        expires_at=value.expires_at,
        disabled_at=value.disabled_at,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _proposal_not_found(error: Exception) -> ProblemException:
    """Proposal/Integration/policy の不存在と越権を同じ 404 へ畳む。"""

    return ProblemException(
        status=404,
        title="Effect resource not found",
        detail=str(error),
        code="effect_resource_not_found",
    )


def _proposal_expired(error: Exception) -> ProblemException:
    """期限切れ approval を 409 Problem へ変換する。"""

    return ProblemException(
        status=409,
        title="ChangeProposal expired",
        detail=str(error),
        code="change_proposal_expired",
    )


def _proposal_conflict(error: Exception) -> ProblemException:
    """Version/checksum/idempotency 競合を 409 Problem へ変換する。"""

    return ProblemException(
        status=409,
        title="ChangeProposal conflict",
        detail=str(error),
        code="change_proposal_conflict",
    )


def _proposal_approval_forbidden(error: Exception) -> ProblemException:
    """Run initiator/ADMIN 以外の effect decision を安定した 403 へ変換する。"""

    return ProblemException(
        status=403,
        title="Effect approval denied",
        detail=str(error),
        code="change_proposal_approval_forbidden",
    )


def _proposal_rejected(error: Exception) -> ProblemException:
    """Scope/policy/Provider gate の拒否を 422 Problem へ変換する。"""

    return ProblemException(
        status=422,
        title="Controlled effect rejected",
        detail=str(error),
        code="controlled_effect_rejected",
    )
