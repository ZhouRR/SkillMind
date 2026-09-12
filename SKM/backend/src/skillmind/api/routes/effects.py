"""ChangeProposal decision と ADMIN preauthorization policy API を提供する。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from skillmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    ProjectWriteActor,
    ReadActor,
    WriteActor,
    authentication_required_problem,
    authorize_project_access,
    csrf_rejected_problem,
    project_archived_problem,
    project_not_found_problem,
    user_access,
)
from skillmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from skillmind.api.release_features import require_deferred_features
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
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
from skillmind.effects.reconciliation_request_service import ReconciliationRequestService
from skillmind.effects.reconciliation_requests import (
    ReconciliationRequestConflictError,
    ReconciliationRequestNotFoundError,
    ReconciliationRequestSnapshot,
)
from skillmind.effects.release import ExecutionFeatureDisabledError
from skillmind.effects.service import EffectService
from skillmind.integrations import (
    IntegrationNotFoundError,
    IntegrationValidationError,
    ensure_explicit_scope,
)
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.service import RunService

router = APIRouter()


class ReconciliationRequestBody(BaseModel):
    """元 UUID 以外の会話/接続/観測を HTTP から受け取らない。"""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class ReconciliationResponse(BaseModel):
    """現在の読取者へ公開できる観測要約だけを明示投影する。"""

    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    project_id: UUID
    run_id: UUID
    effect_execution_id: UUID
    kind: Literal["DATABASE_TRANSACTION", "DOCUMENT_OBJECT"]
    status: Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "REVOKED"]
    created_at: datetime
    finished_at: datetime | None
    observation_status: Literal["CONFIRMED", "NOT_OBSERVED", "CONFLICT"] | None
    observed_at: datetime | None
    error_code: (
        Literal[
            "lookup_unavailable", "lookup_interrupted", "authorization_revoked", "target_changed"
        ]
        | None
    )


class LatestReconciliationResponse(BaseModel):
    """最新要求だけを返し、空値を原書込の未実行と混同しない。"""

    latest: ReconciliationResponse | None


def reconciliation_response(value: ReconciliationRequestSnapshot) -> ReconciliationResponse:
    """内部参照・owner・checksum・receipt は公開 JSON に入れない。"""
    return ReconciliationResponse.model_validate(
        {
            "request_id": value.request_id,
            "project_id": value.reference.project_id,
            "run_id": value.reference.run_id,
            "effect_execution_id": value.reference.effect_execution_id,
            "kind": value.kind,
            "status": value.status,
            "created_at": value.created_at,
            "finished_at": value.finished_at,
            "observation_status": value.observation_status,
            "observed_at": value.observed_at,
            "error_code": value.error_code,
        }
    )


@contextmanager
def _reconciliation_errors() -> Iterator[None]:
    """原接続/例外本文を返さず、共有認証 Problem と資源の 404/409 へ写像する。"""
    try:
        yield
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ReconciliationRequestNotFoundError as error:
        raise ProblemException(
            status=404,
            title="Reconciliation not found",
            detail="The requested effect or reconciliation is not available.",
            code="reconciliation_not_found",
            headers={"Cache-Control": "no-store"},
        ) from error
    except (ReconciliationRequestConflictError, ValueError) as error:
        raise ProblemException(
            status=409,
            title="Reconciliation request conflicts",
            detail="The original request or effect cannot accept this lookup.",
            code="reconciliation_conflict",
            headers={"Cache-Control": "no-store"},
        ) from error


@router.post(
    "/projects/{project_id}/runs/{run_id}/effects/{effect_execution_id}/reconciliations",
    response_model=ReconciliationResponse,
    status_code=202,
    tags=["effects"],
    responses={
        404: problem_openapi_response("Effect not found"),
        409: problem_openapi_response("Request or effect conflicts"),
        503: problem_openapi_response("Worker dispatch is disabled"),
    },
)
async def request_effect_reconciliation(
    request: Request,
    response: Response,
    project_id: UUID,
    run_id: UUID,
    effect_execution_id: UUID,
    body: ReconciliationRequestBody,
    actor: WriteActor,
) -> ReconciliationResponse:
    """現在会話/CSRF と Project 読取権でだけ受理し、API では外部 I/O を行わない。"""
    service: ReconciliationRequestService = request.app.state.reconciliation_requests
    with _reconciliation_errors():
        # 現在の Project 参照認可を先に行い、归档履歴の読取を write gate で拒否しない。
        await authorize_project_access(request, actor, project_id)
        if not request.app.state.settings.worker_dispatch_enabled:
            raise ProblemException(
                status=503,
                title="Reconciliation unavailable",
                detail="Worker dispatch is disabled in this deployment.",
                code="reconciliation_unavailable",
                headers={"Cache-Control": "no-store"},
            )
        value = await service.accept(
            access=user_access(request, actor),
            request_id=body.request_id,
            project_id=project_id,
            run_id=run_id,
            effect_execution_id=effect_execution_id,
        )
    response.headers["Cache-Control"] = "no-store"
    return reconciliation_response(value)


@router.get(
    "/projects/{project_id}/runs/{run_id}/effects/{effect_execution_id}/reconciliations/latest",
    response_model=LatestReconciliationResponse,
    tags=["effects"],
    responses={404: problem_openapi_response("Effect not found")},
)
async def latest_effect_reconciliation(
    request: Request,
    response: Response,
    project_id: UUID,
    run_id: UUID,
    effect_execution_id: UUID,
    actor: ReadActor,
) -> LatestReconciliationResponse:
    """現在の Project 読取権で最新要求だけを確認する。"""
    service: ReconciliationRequestService = request.app.state.reconciliation_requests
    with _reconciliation_errors():
        value = await service.latest(
            access=user_access(request, actor),
            project_id=project_id,
            run_id=run_id,
            effect_execution_id=effect_execution_id,
        )
    response.headers["Cache-Control"] = "no-store"
    return LatestReconciliationResponse(
        latest=None if value is None else reconciliation_response(value)
    )


@router.get(
    "/projects/{project_id}/runs/{run_id}/effect-reconciliations/{request_id}",
    response_model=ReconciliationResponse,
    tags=["effects"],
    responses={404: problem_openapi_response("Reconciliation not found")},
)
async def confirm_effect_reconciliation(
    request: Request,
    response: Response,
    project_id: UUID,
    run_id: UUID,
    request_id: UUID,
    actor: ReadActor,
) -> ReconciliationResponse:
    """受理応答を失った元 UUID を現在の参照権で確認する。"""
    service: ReconciliationRequestService = request.app.state.reconciliation_requests
    with _reconciliation_errors():
        value = await service.confirm(
            access=user_access(request, actor),
            project_id=project_id,
            run_id=run_id,
            request_id=request_id,
        )
    response.headers["Cache-Control"] = "no-store"
    return reconciliation_response(value)


class ChangeProposalResponse(BaseModel):
    """Secret/config/request fingerprint を除外した ChangeProposal response。"""

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

    @model_validator(mode="after")
    def validate_integration_kind(self) -> Self:
        """内部文書庫以外の欠落 Integration と、文書庫の偽接続を公開しない。"""

        if self.capability_version == "document.write/v1":
            if self.integration_id is not None or self.operation != "CREATE":
                raise ValueError("Document library proposal target is invalid")
        elif self.integration_id is None:
            raise ValueError("External proposal requires an Integration")
        return self


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
    responses={409: problem_openapi_response(
        "Approval is disabled, expired, or conflicts with the stored proposal",
        headers=NO_STORE_PROBLEM_HEADERS,
    )},
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

    # 配備上限は repository が原提案の能力/操作で検証する。別 capability の switch
    # で先に閉じると、独立して有効な文書庫の承認も拒否してしまう。
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
            ),
            access=user_access(request, actor),
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except ExecutionFeatureDisabledError as error:
        raise ProblemException(
            status=409, title="Feature is not enabled",
            detail="This execution feature is not enabled in this deployment.",
            code="feature_not_enabled", headers={"Cache-Control": "no-store"},
        ) from error
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
    responses={409: problem_openapi_response(
        "Deferred execution features are disabled", headers=NO_STORE_PROBLEM_HEADERS
    )},
)
async def create_preauthorization(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: CreatePreauthorizationRequest,
) -> PreauthorizationResponse:
    """ADMIN が LOW risk の exact scope だけを事前許可する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    require_deferred_features(request)
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
