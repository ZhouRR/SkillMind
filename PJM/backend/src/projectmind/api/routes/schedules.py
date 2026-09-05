"""Project 作用域の TaskSchedule API route を提供する (計画 §22)。

調度は Run と同じ Project 授権境界に載せる。存在しない schedule と越権は同じ 404 に畳み、
unsafe request は `ProjectWriteActor` で CSRF と membership を同時に検証する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from projectmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    authorize_project_access,
)
from projectmind.api.problems import ProblemException
from projectmind.schedules import (
    PREVIEW_OCCURRENCE_COUNT,
    InvalidScheduleTransitionError,
    ScheduleConflictError,
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleKind,
    ScheduleNotFoundError,
    ScheduleOutcome,
    ScheduleRecord,
    ScheduleService,
    ScheduleStatus,
    build_definition,
)

router = APIRouter()


class ScheduleDefinitionRequest(BaseModel):
    """発火形態の入力。kind ごとの必須組み合わせは service 側の単一検証が判定する。"""

    model_config = ConfigDict(extra="forbid")

    kind: ScheduleKind
    timezone: str = Field(min_length=1, max_length=64)
    cron_expression: str | None = Field(default=None, max_length=128)
    run_at: datetime | None = None
    end_at: datetime | None = None
    max_runs: int | None = Field(default=None, ge=1, le=100_000)


class CreateScheduleRequest(BaseModel):
    """Project 内に schedule を新規作成する request body。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    definition: ScheduleDefinitionRequest
    skill_version_id: UUID
    task_key: str = Field(min_length=1, max_length=200)
    input: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict, max_length=50)


class UpdateScheduleRequest(BaseModel):
    """既存 schedule の定義・名称・凍結入力を差し替える request body。

    束縛する SkillVersion と task は変更させない。別の task を回したいなら別の schedule であり、
    同じ行の履歴 (run_count・last_run) を引き継ぐと何を何回流したのかが読めなくなる。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    definition: ScheduleDefinitionRequest
    input: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict, max_length=50)
    expected_row_version: int = Field(ge=1)


class ScheduleStatusRequest(BaseModel):
    """暂停/恢复/归档の遷移要求。"""

    model_config = ConfigDict(extra="forbid")

    status: ScheduleStatus


class SchedulePreviewRequest(BaseModel):
    """保存前に次回発火時刻を確認する request body。"""

    model_config = ConfigDict(extra="forbid")

    definition: ScheduleDefinitionRequest


class SchedulePreviewResponse(BaseModel):
    """次の発火時刻の予告。空なら保存も通らない。"""

    occurrences: list[datetime]


class ScheduleResponse(BaseModel):
    """schedule の公開 response。接続情報も Secret も含まない。"""

    schedule_id: UUID
    project_id: UUID
    name: str
    kind: ScheduleKind
    status: ScheduleStatus
    timezone: str
    cron_expression: str | None
    run_at: datetime | None
    end_at: datetime | None
    max_runs: int | None
    skill_version_id: UUID
    task_key: str
    input: dict[str, Any]
    sources: dict[str, str]
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: UUID | None
    last_outcome: ScheduleOutcome | None
    last_error: str | None
    run_count: int
    missed_count: int
    created_by: UUID
    row_version: int
    created_at: datetime
    updated_at: datetime


class ScheduleListResponse(BaseModel):
    """Project 内 schedule の一覧ページ。"""

    schedules: list[ScheduleResponse]
    total: int
    limit: int
    offset: int


@router.get(
    "/projects/{project_id}/schedules",
    response_model=ScheduleListResponse,
    tags=["schedules"],
)
async def list_schedules(
    request: Request,
    project_id: UUID,
    actor: ProjectReadActor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ScheduleListResponse:
    """Project access 検証後の schedule 一覧だけを返す。"""

    del actor
    service: ScheduleService = request.app.state.schedule_service
    page = await service.list_schedules(project_id=project_id, limit=limit, offset=offset)
    return ScheduleListResponse(
        schedules=[_schedule_response(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/projects/{project_id}/schedules/preview",
    response_model=SchedulePreviewResponse,
    responses={422: {"description": "Schedule definition is invalid"}},
    tags=["schedules"],
)
async def preview_schedule(
    request: Request,
    project_id: UUID,
    body: SchedulePreviewRequest,
    actor: ProjectWriteActor,
) -> SchedulePreviewResponse:
    """保存せずに次の発火時刻を返す。定義の検証は保存経路と同一。"""

    del actor
    service: ScheduleService = request.app.state.schedule_service
    definition = _definition(body.definition)
    occurrences = service.preview(definition)
    if not occurrences:
        raise _invalid_problem(
            ScheduleInvalidError("Schedule has no future occurrence"),
        )
    return SchedulePreviewResponse(occurrences=occurrences[:PREVIEW_OCCURRENCE_COUNT])


@router.post(
    "/projects/{project_id}/schedules",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"description": "Schedule definition or task configuration is invalid"}},
    tags=["schedules"],
)
async def create_schedule(
    request: Request,
    project_id: UUID,
    body: CreateScheduleRequest,
    actor: ProjectWriteActor,
) -> ScheduleResponse:
    """定義・task・資源選択をすべて検証してから schedule を作成する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: ScheduleService = request.app.state.schedule_service
    definition = _definition(body.definition)
    try:
        record = await service.create_schedule(
            project_id=project_id,
            actor=actor,
            name=body.name,
            definition=definition,
            skill_version_id=body.skill_version_id,
            task_key=body.task_key,
            input_json=body.input,
            sources=body.sources,
        )
    except ScheduleInvalidError as error:
        raise _invalid_problem(error) from error
    return _schedule_response(record)


@router.get(
    "/projects/{project_id}/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    responses={404: {"description": "Schedule not found"}},
    tags=["schedules"],
)
async def get_schedule(
    request: Request,
    project_id: UUID,
    schedule_id: UUID,
    actor: ProjectReadActor,
) -> ScheduleResponse:
    """Project 境界内の schedule を返す。"""

    del actor
    service: ScheduleService = request.app.state.schedule_service
    try:
        record = await service.get_schedule(project_id=project_id, schedule_id=schedule_id)
    except ScheduleNotFoundError as error:
        raise _not_found_problem() from error
    return _schedule_response(record)


@router.put(
    "/projects/{project_id}/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    responses={
        404: {"description": "Schedule not found"},
        409: {"description": "Schedule was modified by another request"},
        422: {"description": "Schedule definition or task configuration is invalid"},
    },
    tags=["schedules"],
)
async def update_schedule(
    request: Request,
    project_id: UUID,
    schedule_id: UUID,
    body: UpdateScheduleRequest,
    actor: ProjectWriteActor,
) -> ScheduleResponse:
    """定義と凍結入力を差し替える。楽観ロックの不一致は 409 にする。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: ScheduleService = request.app.state.schedule_service
    definition = _definition(body.definition)
    try:
        record = await service.update_schedule(
            project_id=project_id,
            schedule_id=schedule_id,
            name=body.name,
            definition=definition,
            input_json=body.input,
            sources=body.sources,
            expected_row_version=body.expected_row_version,
        )
    except ScheduleNotFoundError as error:
        raise _not_found_problem() from error
    except ScheduleConflictError as error:
        raise ProblemException(
            status=409,
            title="Schedule conflict",
            detail=str(error),
            code="schedule_conflict",
        ) from error
    except ScheduleInvalidError as error:
        raise _invalid_problem(error) from error
    return _schedule_response(record)


@router.post(
    "/projects/{project_id}/schedules/{schedule_id}/status",
    response_model=ScheduleResponse,
    responses={
        404: {"description": "Schedule not found"},
        409: {"description": "Schedule transition is not allowed"},
        422: {"description": "Schedule has no future occurrence"},
    },
    tags=["schedules"],
)
async def change_schedule_status(
    request: Request,
    project_id: UUID,
    schedule_id: UUID,
    body: ScheduleStatusRequest,
    actor: ProjectWriteActor,
) -> ScheduleResponse:
    """暂停・恢复・归档を状態機経由で適用する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: ScheduleService = request.app.state.schedule_service
    try:
        record = await service.change_status(
            project_id=project_id, schedule_id=schedule_id, target=body.status
        )
    except ScheduleNotFoundError as error:
        raise _not_found_problem() from error
    except InvalidScheduleTransitionError as error:
        raise ProblemException(
            status=409,
            title="Schedule transition is not allowed",
            detail=str(error),
            code="schedule_transition_invalid",
        ) from error
    except ScheduleInvalidError as error:
        raise _invalid_problem(error) from error
    return _schedule_response(record)


def _definition(body: ScheduleDefinitionRequest) -> ScheduleDefinition:
    """request の発火形態を検証済み定義へ変換する。"""

    try:
        return build_definition(
            kind=body.kind.value,
            timezone=body.timezone,
            cron_expression=body.cron_expression,
            run_at=body.run_at,
            end_at=body.end_at,
            max_runs=body.max_runs,
        )
    except ScheduleInvalidError as error:
        raise _invalid_problem(error) from error


def _invalid_problem(error: ScheduleInvalidError) -> ProblemException:
    """定義・task 設定の不備を単一の 422 へ畳む。"""

    return ProblemException(
        status=422,
        title="Schedule definition is invalid",
        detail=str(error),
        code="schedule_invalid",
    )


def _not_found_problem() -> ProblemException:
    """不存在と越権を同じ 404 へ畳む。"""

    return ProblemException(
        status=404,
        title="Schedule not found",
        detail="Schedule does not exist or is not accessible.",
        code="schedule_not_found",
    )


def _schedule_response(record: ScheduleRecord) -> ScheduleResponse:
    """read model を公開 response へ写す。"""

    return ScheduleResponse(
        schedule_id=record.schedule_id,
        project_id=record.project_id,
        name=record.name,
        kind=record.kind,
        status=record.status,
        timezone=record.timezone,
        cron_expression=record.cron_expression,
        run_at=record.run_at,
        end_at=record.end_at,
        max_runs=record.max_runs,
        skill_version_id=record.skill_version_id,
        task_key=record.task_key,
        input=record.input_json,
        sources=record.sources,
        next_run_at=record.next_run_at,
        last_run_at=record.last_run_at,
        last_run_id=record.last_run_id,
        last_outcome=record.last_outcome,
        last_error=record.last_error,
        run_count=record.run_count,
        missed_count=record.missed_count,
        created_by=record.created_by,
        row_version=record.row_version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
