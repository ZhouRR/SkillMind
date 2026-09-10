"""Project 作用域の TaskSchedule API route を提供する (計画 §22)。

調度は Run と同じ Project 授権境界に載せる。存在しない schedule と越権は同じ 404 に畳み、
unsafe request は `ProjectWriteActor` で CSRF と membership を同時に検証する。
"""

from __future__ import annotations

from typing import Annotated, Any, Self
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from skillmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    authentication_required_problem,
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
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.schedules import (
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
from skillmind.schedules.domain import (
    ScheduleActivity,
    ScheduleActivityUnavailableError,
    ScheduleTracking,
)

router = APIRouter()

# 入口 dependency と保存 transaction の拒否を同じ公開契約で宣言する。
_WRITE_ACCESS_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: problem_openapi_response(description, headers=NO_STORE_PROBLEM_HEADERS)
    for code, description in (
        (401, "The original authenticated session must remain valid"),
        (403, "The request origin or original session CSRF token was rejected"),
        (404, "The project or schedule is not accessible"),
    )
}


class ScheduleDefinitionRequest(BaseModel):
    """発火形態の入力。kind ごとの必須組み合わせは service 側の単一検証が判定する。"""

    model_config = ConfigDict(extra="forbid")

    kind: ScheduleKind
    timezone: str = Field(min_length=1, max_length=64)
    cron_expression: str | None = Field(default=None, max_length=128)
    run_at: AwareDatetime | None = Field(
        default=None, description="Instant with an explicit UTC offset"
    )
    end_at: AwareDatetime | None = Field(
        default=None, description="Instant with an explicit UTC offset"
    )
    max_runs: int | None = Field(default=None, ge=1, le=100_000, strict=True)


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
    expected_row_version: int = Field(ge=1, strict=True)


class ScheduleStatusRequest(BaseModel):
    """表示した原版に対する暂停/恢复/归档の遷移要求。"""

    model_config = ConfigDict(extra="forbid")

    status: ScheduleStatus
    expected_row_version: int = Field(ge=1, strict=True)


class SchedulePreviewRequest(BaseModel):
    """保存前に次回発火時刻を確認する request body。"""

    model_config = ConfigDict(extra="forbid")

    definition: ScheduleDefinitionRequest


class SchedulePreviewResponse(BaseModel):
    """次の発火時刻の予告。空なら保存も通らない。"""

    occurrences: list[AwareDatetime] = Field(min_length=1, max_length=PREVIEW_OCCURRENCE_COUNT)


class ScheduleResponse(BaseModel):
    """schedule の公開 response。接続情報も Secret も含まない。"""

    model_config = ConfigDict(extra="forbid")

    schedule_id: UUID
    project_id: UUID
    name: str = Field(min_length=1)
    kind: ScheduleKind
    status: ScheduleStatus
    timezone: str = Field(min_length=1)
    cron_expression: str | None
    run_at: AwareDatetime | None
    end_at: AwareDatetime | None
    max_runs: int | None = Field(ge=1, strict=True)
    skill_version_id: UUID
    task_key: str = Field(min_length=1)
    input: dict[str, Any]
    sources: dict[str, str]
    next_run_at: AwareDatetime | None
    last_run_at: AwareDatetime | None
    last_run_id: UUID | None
    last_outcome: ScheduleOutcome | None
    last_error: str | None
    run_count: int = Field(ge=0, strict=True)
    missed_count: int = Field(ge=0, strict=True)
    created_by: UUID
    row_version: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ScheduleListResponse(BaseModel):
    """Project 内 schedule の一覧ページ。"""

    model_config = ConfigDict(extra="forbid")

    schedules: list[ScheduleResponse] = Field(max_length=100)
    total: int = Field(ge=0, strict=True)
    limit: int = Field(ge=1, le=100, strict=True)
    offset: int = Field(ge=0, strict=True)


class SchedulePendingOccurrenceResponse(BaseModel):
    """在途一件の観察値だけを返し、期限を実行停止や再送許可と同一視しない。"""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: UUID
    occurrence_at: AwareDatetime
    configuration_version: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    attempt_count: int = Field(ge=1, strict=True)
    lease_expires_at: AwareDatetime


class ScheduleActivityResponse(BaseModel):
    """原 Schedule と同一 SELECT の PENDING 観察であり、完全履歴ではない。"""

    model_config = ConfigDict(extra="forbid")

    schedule_id: UUID
    project_id: UUID
    row_version: int = Field(ge=1, strict=True)
    configuration_version: int = Field(ge=1, strict=True)
    tracking: ScheduleTracking
    checked_at: AwareDatetime
    automatic_attempt_limit: int = Field(ge=1, strict=True)
    pending: SchedulePendingOccurrenceResponse | None

    @model_validator(mode="after")
    def require_tracked_pending(self) -> Self:
        """旧形式や親より未来の設定を追跡済み台帳と見せず、原関係を維持する。"""

        if self.tracking is ScheduleTracking.LEGACY_UNAVAILABLE and self.pending is not None:
            raise ValueError("Legacy schedule cannot expose a tracked pending occurrence")
        if (
            self.pending is not None
            and self.pending.configuration_version > self.configuration_version
        ):
            raise ValueError("Pending configuration cannot exceed the observed schedule version")
        return self


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
    q: Annotated[
        str | None,
        Query(
            max_length=200,
            description=(
                "Literal case-insensitive substring of name or task_key; "
                "whitespace-only means no filter"
            ),
        ),
    ] = None,
    status: Annotated[
        ScheduleStatus | None,
        Query(description="One schedule status; omitted includes all statuses"),
    ] = None,
) -> ScheduleListResponse:
    """Project access 検証後の schedule 一覧だけを返す。"""

    del actor
    for field in ("q", "status"):
        if len(request.query_params.getlist(field)) > 1:
            raise RequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("query", field),
                        "msg": "At most one filter value is allowed",
                    }
                ]
            )
    if q is not None and "\x00" in q:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "q"),
                    "msg": "Search must not contain a NUL character",
                }
            ]
        )
    service: ScheduleService = request.app.state.schedule_service
    page = await service.list_schedules(
        project_id=project_id,
        limit=limit,
        offset=offset,
        q=q,
        status=status,
    )
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
    responses={
        **_WRITE_ACCESS_PROBLEMS,
        201: {"headers": NO_STORE_PROBLEM_HEADERS},
        409: problem_openapi_response("Project is archived", headers=NO_STORE_PROBLEM_HEADERS),
        422: problem_openapi_response(
            "Schedule definition or task configuration is invalid", headers=NO_STORE_PROBLEM_HEADERS
        ),
    },
    tags=["schedules", "auth"],
)
async def create_schedule(
    request: Request,
    project_id: UUID,
    body: CreateScheduleRequest,
    actor: ProjectWriteActor,
) -> ScheduleResponse:
    """定義・task・資源選択をすべて検証してから schedule を作成する。"""

    service: ScheduleService = request.app.state.schedule_service
    definition = _definition(body.definition)
    try:
        record = await service.create_schedule(
            project_id=project_id,
            access=user_access(request, actor),
            name=body.name,
            definition=definition,
            skill_version_id=body.skill_version_id,
            task_key=body.task_key,
            input_json=body.input,
            sources=body.sources,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
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
        **_WRITE_ACCESS_PROBLEMS,
        200: {"headers": NO_STORE_PROBLEM_HEADERS},
        409: problem_openapi_response(
            "Project is archived or schedule was modified by another request",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "Schedule definition or task configuration is invalid", headers=NO_STORE_PROBLEM_HEADERS
        ),
    },
    tags=["schedules", "auth"],
)
async def update_schedule(
    request: Request,
    project_id: UUID,
    schedule_id: UUID,
    body: UpdateScheduleRequest,
    actor: ProjectWriteActor,
) -> ScheduleResponse:
    """定義と凍結入力を差し替える。楽観ロックの不一致は 409 にする。"""

    service: ScheduleService = request.app.state.schedule_service
    definition = _definition(body.definition)
    try:
        record = await service.update_schedule(
            project_id=project_id,
            access=user_access(request, actor),
            schedule_id=schedule_id,
            name=body.name,
            definition=definition,
            input_json=body.input,
            sources=body.sources,
            expected_row_version=body.expected_row_version,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except ScheduleNotFoundError as error:
        raise _not_found_problem() from error
    except ScheduleConflictError as error:
        raise _conflict_problem(error) from error
    except ScheduleInvalidError as error:
        raise _invalid_problem(error) from error
    return _schedule_response(record)


@router.get(
    "/projects/{project_id}/schedules/{schedule_id}/activity",
    response_model=ScheduleActivityResponse,
    responses={
        200: {"headers": NO_STORE_PROBLEM_HEADERS},
        **{
            code: problem_openapi_response(description, headers=NO_STORE_PROBLEM_HEADERS)
            for code, description in (
                (401, "A current authenticated session is required"),
                (404, "Project or schedule is not accessible"),
                (409, "Stored schedule activity cannot be verified"),
                (422, "The project or schedule identity is invalid"),
            )
        },
    },
    tags=["schedules", "auth"],
)
async def get_schedule_activity(
    request: Request,
    project_id: UUID,
    schedule_id: UUID,
    actor: ProjectReadActor,
) -> ScheduleActivityResponse:
    """帰档・作成者失効でも現在の読者資格で観察し、元の Worker 権限は復活させない。"""

    del actor
    service: ScheduleService = request.app.state.schedule_service
    try:
        activity = await service.get_activity(project_id=project_id, schedule_id=schedule_id)
    except ScheduleNotFoundError as error:
        raise _not_found_problem() from error
    except ScheduleActivityUnavailableError as error:
        raise ProblemException(
            status=409,
            title="Schedule activity is unavailable",
            detail="The stored schedule activity cannot be verified. No retry is authorized.",
            code="schedule_activity_unavailable",
        ) from error
    return _activity_response(activity)


@router.post(
    "/projects/{project_id}/schedules/{schedule_id}/status",
    response_model=ScheduleResponse,
    responses={
        **_WRITE_ACCESS_PROBLEMS,
        200: {"headers": NO_STORE_PROBLEM_HEADERS},
        409: problem_openapi_response(
            "Project is archived, schedule changed, or transition is not allowed",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        422: problem_openapi_response(
            "Schedule has no future occurrence or the request is invalid",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["schedules", "auth"],
)
async def change_schedule_status(
    request: Request,
    project_id: UUID,
    schedule_id: UUID,
    body: ScheduleStatusRequest,
    actor: ProjectWriteActor,
) -> ScheduleResponse:
    """暂停・恢复・归档を状態機経由で適用する。"""

    service: ScheduleService = request.app.state.schedule_service
    try:
        record = await service.change_status(
            project_id=project_id,
            access=user_access(request, actor),
            schedule_id=schedule_id,
            target=body.status,
            expected_row_version=body.expected_row_version,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except ScheduleNotFoundError as error:
        raise _not_found_problem() from error
    except InvalidScheduleTransitionError as error:
        raise ProblemException(
            status=409,
            title="Schedule transition is not allowed",
            detail=str(error),
            code="schedule_transition_invalid",
        ) from error
    except ScheduleConflictError as error:
        raise _conflict_problem(error) from error
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


def _conflict_problem(error: ScheduleConflictError) -> ProblemException:
    """定義/状態の同時変更を同じ公開 conflict として返す。"""

    return ProblemException(
        status=409, title="Schedule conflict", detail=str(error), code="schedule_conflict"
    )


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


def _activity_response(record: ScheduleActivity) -> ScheduleActivityResponse:
    """内部 snapshot/worker/token/hash/key を属性列挙で除外して返す。"""

    pending = record.pending
    return ScheduleActivityResponse(
        schedule_id=record.schedule_id,
        project_id=record.project_id,
        row_version=record.row_version,
        configuration_version=record.configuration_version,
        tracking=record.tracking,
        checked_at=record.checked_at,
        automatic_attempt_limit=record.automatic_attempt_limit,
        pending=(
            SchedulePendingOccurrenceResponse(
                occurrence_id=pending.occurrence_id,
                occurrence_at=pending.occurrence_at,
                configuration_version=pending.configuration_version,
                created_at=pending.created_at,
                updated_at=pending.updated_at,
                attempt_count=pending.attempt_count,
                lease_expires_at=pending.lease_expires_at,
            )
            if pending is not None
            else None
        ),
    )
