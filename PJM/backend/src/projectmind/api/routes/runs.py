"""認証済み actor と Project access を強制する Run API route を提供する。"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from projectmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    ReadActor,
    WriteActor,
    authorize_project_access,
)
from projectmind.api.problems import ProblemException
from projectmind.api.routes.effects import (
    ChangeApprovalResponse,
    ChangeProposalResponse,
    EffectExecutionResponse,
    approval_response,
    effect_execution_response,
    proposal_response,
)
from projectmind.auth.service import AuthenticatedActor
from projectmind.documents.snapshot import DocumentSelectionMode
from projectmind.runs.domain import (
    AgentSessionKind,
    CancelledRun,
    CreatedRun,
    IdempotencyConflictError,
    InteractionConflictError,
    InteractionExpiredError,
    InteractionNotFoundError,
    InteractionResponseInvalidError,
    RespondedInteraction,
    RunCancellationState,
    RunDetail,
    RunHistoryPage,
    RunNotCancellableError,
    RunNotFoundError,
    RunStatus,
    SessionContinuationMode,
    TaskSourceSelectionError,
)
from projectmind.runs.resource_projection import (
    DocumentSnapshotStatus,
    RunDocumentSnapshot,
    document_snapshots,
    source_summaries,
)
from projectmind.runs.service import RunService
from projectmind.skills import (
    PublishedTaskNotFoundError,
    SkillService,
    TaskInputInvalidError,
)

router = APIRouter()


class CreateTaskRunRequest(BaseModel):
    """PUBLISHED task descriptor から通用 Run を作成する request body。"""

    model_config = ConfigDict(extra="forbid")

    skill_version_id: UUID
    task_key: str = Field(min_length=1, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict, max_length=50)


class RunResponse(BaseModel):
    """Run 作成と idempotent replay で共通利用する response。"""

    run_id: UUID
    project_id: UUID
    task_id: UUID
    status: RunStatus
    row_version: int
    created_at: datetime
    idempotent_replay: bool


class CancelRunResponse(BaseModel):
    """取消要求の受付状態と現在の Run snapshot を返す response。"""

    run_id: UUID
    project_id: UUID
    status: RunStatus
    row_version: int
    cancellation: RunCancellationState


class RunResultResponse(BaseModel):
    """検証済み Result と validation metadata の公開 response。"""

    result_id: UUID
    output_schema: str
    result_kind: str
    data: dict[str, Any]
    evidence_refs: list[str]
    artifact_refs: list[str]
    change_proposal_refs: list[str]
    optional_schema_identity: dict[str, Any]
    summary: str
    confidence: float | None
    needs_review: bool
    usage: dict[str, Any]
    cost: dict[str, Any]
    validation: dict[str, Any]
    created_at: datetime


class ToolCallSummaryResponse(BaseModel):
    """Raw request/result を除外した ToolCall audit summary。"""

    tool_call_id: UUID
    run_attempt_id: UUID
    agent_session_id: UUID
    tool_name: str
    capability: str
    provider: str
    arguments_summary: dict[str, Any]
    status: str
    duration_ms: int | None
    created_at: datetime


class EvidenceResponse(BaseModel):
    """Run ownership 確認後に公開する Evidence locator と excerpt。"""

    evidence_ref: str
    tool_call_id: UUID
    evidence_type: str
    source_uri: str
    source_locator: dict[str, Any]
    content_hash: str
    snapshot_uri: str | None
    excerpt: str | None
    metadata: dict[str, Any]
    created_at: datetime


class RunSkillSnapshotResponse(BaseModel):
    """Run が実際に使用する published SkillVersion binding。"""

    skill_version_id: UUID
    sort_order: int
    manifest_checksum: str
    config_snapshot: dict[str, Any]


class RunSegmentResponse(BaseModel):
    """Run の業務継続単位と Brief checkpoint の公開 projection。"""

    run_segment_id: UUID | None
    segment_no: int
    trigger_type: str
    trigger_ref: UUID | None
    status: str
    objective: dict[str, Any]
    checkpoint: dict[str, Any]
    continuation_mode: SessionContinuationMode
    parent_agent_session_id: UUID | None
    task_brief_checksum: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunAttemptResponse(BaseModel):
    """Lease credential を除外した Segment 内の技術試行。"""

    run_attempt_id: UUID
    run_segment_id: UUID | None
    attempt_no: int
    reason: str
    status: str
    worker_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    created_at: datetime


class AgentSessionResponse(BaseModel):
    """順次 AgentSession の lineage と engine identity。"""

    agent_session_id: UUID
    run_segment_id: UUID | None
    run_attempt_id: UUID
    sdk_session_id: UUID | None
    parent_session_id: UUID | None
    continuation_mode: SessionContinuationMode
    session_kind: AgentSessionKind
    checkpoint_checksum: str | None
    engine_options_checksum: str | None
    engine: str
    sdk_version: str
    cli_version: str
    model: str
    status: str
    usage: dict[str, Any]
    cost: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class InteractionResponseDetail(BaseModel):
    """公開 Interaction に追加された actor 回答。"""

    response_id: UUID
    actor_id: UUID
    interaction_version: int
    response: dict[str, Any]
    created_at: datetime


class UserInteractionResponse(BaseModel):
    """Run detail に表示する公開質問、選択肢、期限と回答。"""

    interaction_id: UUID
    run_segment_id: UUID
    agent_session_id: UUID
    interaction_type: str
    prompt: dict[str, Any]
    options: list[dict[str, Any]]
    required: bool
    expires_at: datetime
    status: str
    version: int
    continuation_mode: SessionContinuationMode
    checkpoint_checksum: str
    change_proposal_id: UUID | None
    response: InteractionResponseDetail | None
    created_at: datetime


class InteractionAnswer(BaseModel):
    """CLARIFICATION/CHOICE/REVIEW 共通の追加式回答 payload。"""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=10_000)
    selected_option_keys: list[str] | None = Field(default=None, min_length=1, max_length=20)


class RespondInteractionRequest(BaseModel):
    """Interaction version を固定して一回だけ回答する request body。"""

    model_config = ConfigDict(extra="forbid")

    interaction_version: int = Field(ge=1)
    response: InteractionAnswer


class RespondInteractionResponse(BaseModel):
    """回答後に作成した次 Segment と Run snapshot。"""

    run_id: UUID
    project_id: UUID
    status: RunStatus
    row_version: int
    interaction_id: UUID
    response_id: UUID
    run_segment_id: UUID
    segment_no: int
    continuation_mode: SessionContinuationMode
    idempotent_replay: bool


class RunSourceSummaryResponse(BaseModel):
    """一覧と詳細で共有する資源摘要。内部 binding と文書正文は公開しない。"""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    capability: str | None = None
    resource_kind: str | None = None
    access: str | None = None


class FrozenDocumentResponse(BaseModel):
    """Run 作成時の文書 identity と metadata の公開許可リスト。"""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    folder: str
    name: str
    mime: str
    size: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class DocumentSnapshotResponse(BaseModel):
    """Project と requirement を固定した v1 清単。checksum の検証は共通 domain が担う。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_version: Literal["v1"]
    project_id: UUID
    requirement_key: str
    selection_mode: DocumentSelectionMode
    documents: list[FrozenDocumentResponse] = Field(min_length=1, max_length=5000)
    checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class RunDocumentSnapshotResponse(BaseModel):
    """清単を安全に公開できない旧記録/不正記録を、空集合と混同させない。"""

    model_config = ConfigDict(extra="forbid")

    requirement_key: str
    status: DocumentSnapshotStatus
    snapshot: DocumentSnapshotResponse | None


class RunDetailResponse(BaseModel):
    """Project-scoped Run、Result、ToolCall、Evidence の read response。"""

    run_id: UUID
    project_id: UUID
    task_id: UUID
    status: RunStatus
    row_version: int
    created_at: datetime
    input: dict[str, Any]
    selected_sources: dict[str, str | RunSourceSummaryResponse]
    document_snapshots: list[RunDocumentSnapshotResponse]
    output_schema: dict[str, Any] | None
    output_schema_checksum: str | None
    result: RunResultResponse | None
    tool_calls: list[ToolCallSummaryResponse]
    evidence: list[EvidenceResponse]
    skill_snapshots: list[RunSkillSnapshotResponse]
    segments: list[RunSegmentResponse]
    attempts: list[RunAttemptResponse]
    sessions: list[AgentSessionResponse]
    interactions: list[UserInteractionResponse]
    change_proposals: list[ChangeProposalResponse]
    approvals: list[ChangeApprovalResponse]
    effect_executions: list[EffectExecutionResponse]


class RunHistoryItemResponse(BaseModel):
    """Project Run history の一覧と再表示に必要な一行。"""

    run_id: UUID
    project_id: UUID
    task_id: UUID
    status: RunStatus
    row_version: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    input: dict[str, Any]
    selected_sources: dict[str, str | RunSourceSummaryResponse]
    result_summary: str | None
    result_confidence: float | None
    result_needs_review: bool | None


class RunHistoryResponse(BaseModel):
    """Offset pagination metadata を含む Project Run history response。"""

    items: list[RunHistoryItemResponse]
    limit: int
    offset: int
    has_more: bool


@router.post(
    "/projects/{project_id}/task-runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": RunResponse, "description": "Idempotent replay"},
        404: {"description": "Published task not found"},
        409: {"description": "Idempotency conflict"},
        422: {"description": "Task input or data source selection is invalid"},
    },
    tags=["runs"],
)
async def create_task_run(
    request: Request,
    response: Response,
    project_id: UUID,
    body: CreateTaskRunRequest,
    actor: ProjectWriteActor,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> RunResponse:
    """PUBLISHED task を精確 version へ束縛し、schema 検証済み入力で通用 Run を作成する。"""

    skill_service: SkillService = request.app.state.skill_service
    run_service: RunService = request.app.state.run_service
    find_replay = partial(
        run_service.find_task_run_replay,
        project_id=project_id,
        skill_version_id=body.skill_version_id,
        task_key=body.task_key,
        input_json=body.input,
        sources=body.sources,
        actor_id=actor.user_id,
        idempotency_key=idempotency_key,
    )
    try:
        run = await find_replay()
        if run is None:
            try:
                resolved = await skill_service.resolve_task_run(
                    project_id=project_id,
                    skill_version_id=body.skill_version_id,
                    task_key=body.task_key,
                    input_json=body.input,
                )
            except (PublishedTaskNotFoundError, TaskInputInvalidError):
                # 初回照会の後で別要求が commit し、公開状態が失効した競争も同じ原要求へ戻す。
                run = await find_replay()
                if run is None:
                    raise
            else:
                run = await run_service.create_task_run(
                    project_id=project_id,
                    resolved=resolved,
                    input_json=body.input,
                    sources=body.sources,
                    idempotency_key=idempotency_key,
                    trace_id=request.state.request_id,
                    actor_id=actor.user_id,
                    actor_system_role=actor.system_role,
                    project_membership=(
                        "ADMIN_BYPASS" if actor.system_role == "ADMIN" else "ACTIVE"
                    ),
                )
    except PublishedTaskNotFoundError as error:
        raise ProblemException(
            status=404,
            title="Published task not found",
            detail=str(error),
            code="published_task_not_found",
        ) from error
    except TaskInputInvalidError as error:
        raise ProblemException(
            status=422,
            title="Task input is invalid",
            detail=str(error),
            code="task_input_invalid",
        ) from error
    except TaskSourceSelectionError as error:
        raise ProblemException(
            status=422,
            title="Data source selection is invalid",
            detail=str(error),
            code="task_source_selection_invalid",
        ) from error
    except IdempotencyConflictError as error:
        raise ProblemException(
            status=409,
            title="Idempotency conflict",
            detail=str(error),
            code="idempotency_conflict",
        ) from error
    response.status_code = status.HTTP_200_OK if run.idempotent_replay else status.HTTP_201_CREATED
    root_path = request.scope.get("root_path", "")
    response.headers["Location"] = f"{root_path}/api/v1/runs/{run.run_id}"
    response.headers["Idempotent-Replay"] = str(run.idempotent_replay).lower()
    return _run_response(run)


@router.get(
    "/projects/{project_id}/runs",
    response_model=RunHistoryResponse,
    tags=["runs"],
)
async def list_run_history(
    request: Request,
    project_id: UUID,
    actor: ProjectReadActor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[list[RunStatus] | None, Query()] = None,
) -> RunHistoryResponse:
    """Project access 検証後の Run history だけを返す。

    `status` を繰り返すとその状態だけに絞る。回答待ち・承認待ちの一覧はこれで作る——
    画面側で先頭 page を filter すると、待機中の Run が古い page にあるときに取りこぼす。
    """

    del actor
    service: RunService = request.app.state.run_service
    page = await service.list_run_history(
        project_id=project_id,
        limit=limit,
        offset=offset,
        statuses=tuple(status or ()),
    )
    return _run_history_response(page)


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
    responses={404: {"description": "Run not found"}},
    tags=["runs"],
)
async def get_run(
    request: Request,
    run_id: UUID,
    actor: ReadActor,
) -> RunResponse:
    """Run 所属 Project の access を確認して snapshot を返す。"""

    run = await authorized_run(request, actor, run_id)
    return _run_response(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=CancelRunResponse,
    responses={404: {"description": "Run not found"}, 409: {"description": "Not cancellable"}},
    tags=["runs"],
)
async def cancel_run(
    request: Request,
    run_id: UUID,
    actor: WriteActor,
) -> CancelRunResponse:
    """CSRF と Run 所属 Project access を確認して cancellation intent を追加する。"""

    await authorized_run(request, actor, run_id)
    service: RunService = request.app.state.run_service
    try:
        cancelled = await service.cancel_run(run_id, trace_id=request.state.request_id)
    except RunNotFoundError as error:
        raise run_not_found_problem(error) from error
    except RunNotCancellableError as error:
        raise ProblemException(
            status=409,
            title="Run cannot be cancelled",
            detail=str(error),
            code="run_not_cancellable",
        ) from error
    return _cancel_run_response(cancelled)


@router.get(
    "/projects/{project_id}/runs/{run_id}/detail",
    response_model=RunDetailResponse,
    responses={404: {"description": "Run not found in project"}},
    tags=["runs"],
)
async def get_run_detail(
    request: Request,
    project_id: UUID,
    run_id: UUID,
    actor: ProjectReadActor,
) -> RunDetailResponse:
    """Project access と複合 ownership を確認して Result/Evidence を返す。"""

    del actor
    service: RunService = request.app.state.run_service
    try:
        detail = await service.get_run_detail(project_id=project_id, run_id=run_id)
    except RunNotFoundError as error:
        raise run_not_found_problem(error) from error
    return _run_detail_response(detail)


@router.post(
    "/projects/{project_id}/runs/{run_id}/interactions/{interaction_id}/responses",
    response_model=RespondInteractionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": RespondInteractionResponse, "description": "Idempotent replay"},
        404: {"description": "Run or interaction not found"},
        409: {"description": "Interaction state or version conflict"},
        410: {"description": "Interaction expired"},
        422: {"description": "Response does not match interaction type"},
    },
    tags=["runs"],
)
async def respond_to_interaction(
    request: Request,
    response: Response,
    project_id: UUID,
    run_id: UUID,
    interaction_id: UUID,
    body: RespondInteractionRequest,
    actor: ProjectWriteActor,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> RespondInteractionResponse:
    """Project access、version、期限を検証し、回答から次 Segment を作成する。"""

    service: RunService = request.app.state.run_service
    try:
        responded = await service.respond_to_interaction(
            project_id=project_id,
            run_id=run_id,
            interaction_id=interaction_id,
            actor_id=actor.user_id,
            interaction_version=body.interaction_version,
            response_json=body.response.model_dump(exclude_none=True),
            idempotency_key=idempotency_key,
            trace_id=request.state.request_id,
        )
    except InteractionNotFoundError as error:
        raise run_not_found_problem(error) from error
    except InteractionExpiredError as error:
        raise ProblemException(
            status=410,
            title="Interaction expired",
            detail=str(error),
            code="interaction_expired",
        ) from error
    except InteractionConflictError as error:
        raise ProblemException(
            status=409,
            title="Interaction response conflicts",
            detail=str(error),
            code="interaction_conflict",
        ) from error
    except InteractionResponseInvalidError as error:
        raise ProblemException(
            status=422,
            title="Interaction response is invalid",
            detail=str(error),
            code="interaction_response_invalid",
        ) from error
    response.status_code = (
        status.HTTP_200_OK if responded.idempotent_replay else status.HTTP_201_CREATED
    )
    response.headers["Idempotent-Replay"] = str(responded.idempotent_replay).lower()
    return _responded_interaction_response(responded)


async def authorized_run(
    request: Request,
    actor: AuthenticatedActor,
    run_id: UUID,
) -> CreatedRun:
    """Run の存在と所属 Project access を同じ 404 境界で検証する。"""

    service: RunService = request.app.state.run_service
    try:
        run = await service.get_run(run_id)
    except RunNotFoundError as error:
        raise run_not_found_problem(error) from error
    try:
        await authorize_project_access(request, actor, run.project_id)
    except ProblemException as error:
        if error.code == "project_not_found":
            hidden = RunNotFoundError(f"Run not found: {run_id}")
            raise run_not_found_problem(hidden) from error
        raise
    return run


def run_not_found_problem(error: Exception) -> ProblemException:
    """Run の不存在と越権を区別しない 404 Problem を返す。"""

    return ProblemException(
        status=404,
        title="Run not found",
        detail=str(error),
        code="run_not_found",
    )


def _run_response(run: CreatedRun) -> RunResponse:
    """Application DTO を公開 API response へ変換する。"""

    return RunResponse(
        run_id=run.run_id,
        project_id=run.project_id,
        task_id=run.task_id,
        status=run.status,
        row_version=run.row_version,
        created_at=run.created_at,
        idempotent_replay=run.idempotent_replay,
    )


def _cancel_run_response(cancelled: CancelledRun) -> CancelRunResponse:
    """取消 use case の DTO を公開 field の許可リストへ変換する。"""

    return CancelRunResponse(
        run_id=cancelled.run.run_id,
        project_id=cancelled.run.project_id,
        status=cancelled.run.status,
        row_version=cancelled.run.row_version,
        cancellation=cancelled.cancellation,
    )


def _run_detail_response(detail: RunDetail) -> RunDetailResponse:
    """Run read model を raw infrastructure field を含まない API response へ変換する。"""

    result = detail.result
    return RunDetailResponse(
        run_id=detail.run.run_id,
        project_id=detail.run.project_id,
        task_id=detail.run.task_id,
        status=detail.run.status,
        row_version=detail.run.row_version,
        created_at=detail.run.created_at,
        input=detail.input,
        selected_sources=_source_summary_response(detail.selected_sources),
        document_snapshots=[
            _document_snapshot_response(item)
            for item in document_snapshots(
                detail.selected_sources, project_id=detail.run.project_id
            )
        ],
        output_schema=detail.output_schema,
        output_schema_checksum=detail.output_schema_checksum,
        result=None
        if result is None
        else RunResultResponse(
            result_id=result.result_id,
            output_schema=result.output_schema,
            result_kind=result.result_kind,
            data=result.data,
            evidence_refs=list(result.evidence_refs),
            artifact_refs=list(result.artifact_refs),
            change_proposal_refs=list(result.change_proposal_refs),
            optional_schema_identity=result.optional_schema_identity,
            summary=result.summary,
            confidence=result.confidence,
            needs_review=result.needs_review,
            usage=result.usage,
            cost=result.cost,
            validation=result.validation,
            created_at=result.created_at,
        ),
        tool_calls=[
            ToolCallSummaryResponse(
                tool_call_id=item.tool_call_id,
                run_attempt_id=item.run_attempt_id,
                agent_session_id=item.agent_session_id,
                tool_name=item.tool_name,
                capability=item.capability,
                provider=item.provider,
                arguments_summary=item.arguments_summary,
                status=item.status,
                duration_ms=item.duration_ms,
                created_at=item.created_at,
            )
            for item in detail.tool_calls
        ],
        evidence=[
            EvidenceResponse(
                evidence_ref=item.evidence_ref,
                tool_call_id=item.tool_call_id,
                evidence_type=item.evidence_type,
                source_uri=item.source_uri,
                source_locator=item.source_locator,
                content_hash=item.content_hash,
                snapshot_uri=item.snapshot_uri,
                excerpt=item.excerpt,
                metadata=item.metadata,
                created_at=item.created_at,
            )
            for item in detail.evidence
        ],
        skill_snapshots=[
            RunSkillSnapshotResponse(
                skill_version_id=item.skill_version_id,
                sort_order=item.sort_order,
                manifest_checksum=item.manifest_checksum,
                config_snapshot=item.config_snapshot,
            )
            for item in detail.skill_snapshots
        ],
        segments=[
            RunSegmentResponse(
                run_segment_id=item.run_segment_id,
                segment_no=item.segment_no,
                trigger_type=item.trigger_type.value,
                trigger_ref=item.trigger_ref,
                status=item.status.value,
                objective=item.objective,
                checkpoint=item.checkpoint,
                continuation_mode=item.continuation_mode,
                parent_agent_session_id=item.parent_agent_session_id,
                task_brief_checksum=item.task_brief_checksum,
                started_at=item.started_at,
                finished_at=item.finished_at,
                created_at=item.created_at,
            )
            for item in detail.segments
        ],
        attempts=[
            RunAttemptResponse(
                run_attempt_id=item.run_attempt_id,
                run_segment_id=item.run_segment_id,
                attempt_no=item.attempt_no,
                reason=item.reason,
                status=item.status.value,
                worker_id=item.worker_id,
                started_at=item.started_at,
                finished_at=item.finished_at,
                error=item.error,
                created_at=item.created_at,
            )
            for item in detail.attempts
        ],
        sessions=[
            AgentSessionResponse(
                agent_session_id=item.agent_session_id,
                run_segment_id=item.run_segment_id,
                run_attempt_id=item.run_attempt_id,
                sdk_session_id=item.sdk_session_id,
                parent_session_id=item.parent_session_id,
                continuation_mode=item.continuation_mode,
                session_kind=item.session_kind,
                checkpoint_checksum=item.checkpoint_checksum,
                engine_options_checksum=item.engine_options_checksum,
                engine=item.engine,
                sdk_version=item.sdk_version,
                cli_version=item.cli_version,
                model=item.model,
                status=item.status,
                usage=item.usage,
                cost=item.cost,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in detail.sessions
        ],
        interactions=[
            UserInteractionResponse(
                interaction_id=item.interaction_id,
                run_segment_id=item.run_segment_id,
                agent_session_id=item.agent_session_id,
                interaction_type=item.interaction_type.value,
                prompt=item.prompt,
                options=list(item.options),
                required=item.required,
                expires_at=item.expires_at,
                status=item.status.value,
                version=item.version,
                continuation_mode=item.continuation_mode,
                checkpoint_checksum=item.checkpoint_checksum,
                change_proposal_id=item.change_proposal_id,
                response=(
                    None
                    if item.response is None
                    else InteractionResponseDetail(
                        response_id=item.response.response_id,
                        actor_id=item.response.actor_id,
                        interaction_version=item.response.interaction_version,
                        response=item.response.response,
                        created_at=item.response.created_at,
                    )
                ),
                created_at=item.created_at,
            )
            for item in detail.interactions
        ],
        change_proposals=[proposal_response(item) for item in detail.change_proposals],
        approvals=[approval_response(item) for item in detail.approvals],
        effect_executions=[effect_execution_response(item) for item in detail.effect_executions],
    )


def _responded_interaction_response(
    responded: RespondedInteraction,
) -> RespondInteractionResponse:
    """Interaction use case DTO を公開 field の許可リストへ変換する。"""

    return RespondInteractionResponse(
        run_id=responded.run.run_id,
        project_id=responded.run.project_id,
        status=responded.run.status,
        row_version=responded.run.row_version,
        interaction_id=responded.interaction_id,
        response_id=responded.response_id,
        run_segment_id=responded.run_segment_id,
        segment_no=responded.segment_no,
        continuation_mode=responded.continuation_mode,
        idempotent_replay=responded.idempotent_replay,
    )


def _document_snapshot_response(item: RunDocumentSnapshot) -> RunDocumentSnapshotResponse:
    """domain が検証した metadata のみを、明示 field の response model へ渡す。"""

    return RunDocumentSnapshotResponse(
        requirement_key=item.requirement_key,
        status=item.status,
        snapshot=None
        if item.snapshot is None
        else DocumentSnapshotResponse.model_validate(item.snapshot.to_json()),
    )


def _source_summary_response(sources: dict[str, Any]) -> dict[str, str | RunSourceSummaryResponse]:
    """一覧と詳細の両方で同じ公開許可リストを通す。"""

    return {
        key: value if isinstance(value, str) else RunSourceSummaryResponse.model_validate(value)
        for key, value in source_summaries(sources).items()
    }


def _run_history_response(page: RunHistoryPage) -> RunHistoryResponse:
    """Run history read model を公開 pagination response へ変換する。"""

    return RunHistoryResponse(
        items=[
            RunHistoryItemResponse(
                run_id=item.run.run_id,
                project_id=item.run.project_id,
                task_id=item.run.task_id,
                status=item.run.status,
                row_version=item.run.row_version,
                created_at=item.run.created_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
                input=item.input,
                selected_sources=_source_summary_response(item.selected_sources),
                result_summary=item.result_summary,
                result_confidence=item.result_confidence,
                result_needs_review=item.result_needs_review,
            )
            for item in page.items
        ],
        limit=page.limit,
        offset=page.offset,
        has_more=page.has_more,
    )
