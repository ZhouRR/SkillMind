"""認証済み Project CRUD と membership 管理 API を提供する。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from projectmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    ReadActor,
    administrator_required_problem,
    authentication_required_problem,
    csrf_rejected_problem,
    project_not_found_problem,
    user_access,
)
from projectmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.projects import (
    ProjectDeleteBlockedError,
    ProjectKeyConflictError,
    ProjectMemberNotFoundError,
    ProjectMemberStatus,
    ProjectMemberUserNotFoundError,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStatus,
    ProjectVersionConflictError,
    ProjectVersionExhaustedError,
    StoredProject,
    StoredProjectMember,
    UpdateProjectCommand,
)
from projectmind.projects.domain import MAX_PROJECT_VERSION, validate_project_version
from projectmind.users.domain import UserAdministrationDeniedError

router = APIRouter(tags=["projects", "auth"])

_PROJECT_PROBLEMS: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("A valid session is required", headers=NO_STORE_PROBLEM_HEADERS),
    403: problem_openapi_response("ADMIN or CSRF rejected", headers=NO_STORE_PROBLEM_HEADERS),
    404: problem_openapi_response(
        "Project not found or inaccessible", headers=NO_STORE_PROBLEM_HEADERS,
    ),
    422: problem_openapi_response(
        "Invalid Project identity, version or request", headers=NO_STORE_PROBLEM_HEADERS,
    ),
}
_VERSION_CONFLICT = problem_openapi_response(
    "project_version_conflict or project_version_exhausted", headers=NO_STORE_PROBLEM_HEADERS,
)
ProjectVersion = Annotated[int, Field(strict=True, ge=1, le=MAX_PROJECT_VERSION)]


def _query_project_version(value: object) -> int:
    """query の文字列を十進正整数だけとして解釈し、小数/指数/空文字の暗黙変換を拒否する。"""

    if (
        not isinstance(value, str) or not value.isascii()
        or not value.isdecimal() or len(value) > 10
    ):
        raise ValueError("Project version query must be a decimal integer")
    return validate_project_version(int(value))


QueryProjectVersion = Annotated[
    int, BeforeValidator(_query_project_version), Query(ge=1, le=MAX_PROJECT_VERSION),
]

_MEMBER_PROBLEMS: dict[int | str, dict[str, Any]] = {
    401: problem_openapi_response("A valid session is required", headers=NO_STORE_PROBLEM_HEADERS),
    403: problem_openapi_response("ADMIN or CSRF rejected", headers=NO_STORE_PROBLEM_HEADERS),
    404: problem_openapi_response(
        "Project, eligible User or membership not found", headers=NO_STORE_PROBLEM_HEADERS,
    ),
    422: problem_openapi_response(
        "Invalid identity or missing CSRF header", headers=NO_STORE_PROBLEM_HEADERS,
    ),
}


class CreateProjectRequest(BaseModel):
    """Organization 内に Project を作成する request。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    settings: dict[str, Any] = Field(default_factory=dict)
    retention_days: int = Field(ge=1, le=3650)


class UpdateProjectRequest(BaseModel):
    """Project の変更可能な metadata を部分更新する request。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "anyOf": [
                {"required": [field], "properties": {field: {"not": {"type": "null"}}}}
                for field in ("name", "description", "settings", "retention_days")
            ],
        },
    )

    expected_row_version: ProjectVersion
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    settings: dict[str, Any] | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def require_change(self) -> UpdateProjectRequest:
        """空 PATCH を拒否し、監査上意味のない updated_at 変更を防ぐ。"""

        if all(value is None for value in (
            self.name, self.description, self.settings, self.retention_days,
        )):
            raise ValueError("At least one non-null project field must be provided")
        return self


class ProjectVersionRequest(BaseModel):
    """アーカイブ/復元に使う元の版だけを受け取り、現版への自動置換を許さない。"""

    model_config = ConfigDict(extra="forbid")

    expected_row_version: ProjectVersion


class ProjectResponse(BaseModel):
    """内部 organization field を含まない Project response。"""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=4000)
    status: ProjectStatus
    settings: dict[str, Any]
    retention_days: int = Field(ge=1, le=3650)
    row_version: ProjectVersion
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """Actor が参照可能な Project 一覧 response。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ProjectResponse]


class ProjectMemberResponse(BaseModel):
    """Credential を含まない Project membership response。"""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    email: str = Field(max_length=320, json_schema_extra={"format": "email"})
    display_name: str = Field(min_length=1, max_length=200)
    status: ProjectMemberStatus = Field(
        description="Membership state, independent of User account status.",
    )
    joined_at: datetime


class ProjectMemberListResponse(BaseModel):
    """Project membership 一覧 response。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ProjectMemberResponse]


@router.get(
    "/projects", response_model=ProjectListResponse,
    responses={
        200: {"headers": NO_STORE_PROBLEM_HEADERS},
        401: _PROJECT_PROBLEMS[401], 422: _PROJECT_PROBLEMS[422],
    },
)
async def list_projects(
    request: Request,
    actor: ReadActor,
    include_archived: bool = Query(default=False),
) -> ProjectListResponse:
    """ADMIN は組織内、USER は所属 Project の一覧を返す。"""

    service: ProjectService = request.app.state.project_service
    projects = await service.list_projects(actor=actor, include_archived=include_archived)
    return ProjectListResponse(items=[_project_response(project) for project in projects])


@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **_PROJECT_PROBLEMS, 201: {"headers": NO_STORE_PROBLEM_HEADERS},
        409: problem_openapi_response("project_key_conflict", headers=NO_STORE_PROBLEM_HEADERS),
    },
)
async def create_project(
    request: Request,
    actor: AdminWriteActor,
    body: CreateProjectRequest,
) -> ProjectResponse:
    """ADMIN の Organization に新規 Project を作成する。"""

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        project = await service.create_project(
            access=user_access(request, actor),
            key=body.key,
            name=body.name,
            description=body.description,
            settings=body.settings,
            retention_days=body.retention_days,
        )
    return _project_response(project)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    responses={
        200: {"headers": NO_STORE_PROBLEM_HEADERS},
        401: _PROJECT_PROBLEMS[401], 404: _PROJECT_PROBLEMS[404], 422: _PROJECT_PROBLEMS[422],
    },
)
async def get_project(
    request: Request,
    project_id: UUID,
    actor: ReadActor,
) -> ProjectResponse:
    """Actor が参照可能な Project metadata を ARCHIVED を含めて取得する。"""

    service: ProjectService = request.app.state.project_service
    try:
        project = await service.get_project(actor=actor, project_id=project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    return _project_response(project)


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    responses={
        **_PROJECT_PROBLEMS, 200: {"headers": NO_STORE_PROBLEM_HEADERS}, 409: _VERSION_CONFLICT,
    },
)
async def update_project(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: UpdateProjectRequest,
) -> ProjectResponse:
    """ADMIN が Project の変更可能な metadata を更新する。"""

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        project = await service.update_project(
            access=user_access(request, actor),
            project_id=project_id,
            command=UpdateProjectCommand(
                expected_row_version=body.expected_row_version,
                name=body.name,
                description=body.description,
                settings=body.settings,
                retention_days=body.retention_days,
            ),
        )
    return _project_response(project)


@router.post(
    "/projects/{project_id}/archive",
    response_model=ProjectResponse,
    responses={
        **_PROJECT_PROBLEMS, 200: {"headers": NO_STORE_PROBLEM_HEADERS}, 409: _VERSION_CONFLICT,
    },
)
async def archive_project(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: ProjectVersionRequest,
) -> ProjectResponse:
    """ADMIN が Project を物理削除せず ARCHIVED にする。"""

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        project = await service.archive_project(
            access=user_access(request, actor), project_id=project_id,
            expected_row_version=body.expected_row_version,
        )
    return _project_response(project)


@router.post(
    "/projects/{project_id}/unarchive",
    response_model=ProjectResponse,
    responses={
        **_PROJECT_PROBLEMS, 200: {"headers": NO_STORE_PROBLEM_HEADERS}, 409: _VERSION_CONFLICT,
    },
)
async def unarchive_project(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: ProjectVersionRequest,
) -> ProjectResponse:
    """ADMIN が ARCHIVED Project を ACTIVE へ戻す。

    key は archive しても解放しないため、同じ key の作成が 409 になった利用者の復旧経路は
    新規作成ではなくこの復元になる。
    """

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        project = await service.unarchive_project(
            access=user_access(request, actor), project_id=project_id,
            expected_row_version=body.expected_row_version,
        )
    return _project_response(project)


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_PROJECT_PROBLEMS, 204: {"headers": NO_STORE_PROBLEM_HEADERS},
        409: problem_openapi_response(
            "Deletion rejected: project_delete_requires_archive, "
            "project_delete_blocked_by_runs, project_delete_blocked_by_schedules, "
            "project_delete_blocked_by_member_audit, or project_version_conflict",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
)
async def delete_project(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    expected_row_version: QueryProjectVersion,
) -> Response:
    """ADMIN が Run/Schedule/所属監査のない ARCHIVED Project を物理削除し key を解放する。"""

    if len(request.query_params.getlist("expected_row_version")) != 1:
        raise RequestValidationError([{
            "type": "value_error", "loc": ("query", "expected_row_version"),
            "msg": "Exactly one project version is required",
        }])
    service: ProjectService = request.app.state.project_service
    with _project_errors():
        await service.delete_project(
            access=user_access(request, actor), project_id=project_id,
            expected_row_version=expected_row_version,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/members",
    response_model=ProjectMemberListResponse,
    responses={**_MEMBER_PROBLEMS, 200: {"headers": NO_STORE_PROBLEM_HEADERS}},
)
async def list_project_members(
    request: Request,
    project_id: UUID,
    actor: AdminReadActor,
) -> ProjectMemberListResponse:
    """ADMIN に Project membership の一覧を返す。"""

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        members = await service.list_members(
            access=user_access(request, actor), project_id=project_id,
        )
    return ProjectMemberListResponse(items=[_member_response(member) for member in members])


@router.put(
    "/projects/{project_id}/members/{user_id}",
    response_model=ProjectMemberResponse,
    responses={**_MEMBER_PROBLEMS, 200: {"headers": NO_STORE_PROBLEM_HEADERS}},
)
async def add_project_member(
    request: Request,
    project_id: UUID,
    user_id: UUID,
    actor: AdminWriteActor,
) -> ProjectMemberResponse:
    """ADMIN が同一 Organization の ACTIVE User を Project に追加する。"""

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        member = await service.add_member(
            access=user_access(request, actor), project_id=project_id, user_id=user_id,
        )
    return _member_response(member)


@router.delete(
    "/projects/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_MEMBER_PROBLEMS, 204: {"headers": NO_STORE_PROBLEM_HEADERS}},
)
async def remove_project_member(
    request: Request,
    project_id: UUID,
    user_id: UUID,
    actor: AdminWriteActor,
) -> Response:
    """ADMIN が membership を物理削除せず REMOVED にする。"""

    service: ProjectService = request.app.state.project_service
    with _project_errors():
        await service.remove_member(
            access=user_access(request, actor), project_id=project_id, user_id=user_id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@contextmanager
def _project_errors() -> Iterator[None]:
    """入口後の再認証拒否も、資源の存在を漏らさず共通 Problem へ変換する。"""

    try:
        yield
    except ProjectVersionConflictError as error:
        raise ProblemException(
            status=409, title="Project changed",
            detail="Refresh the project before making a new decision.",
            code="project_version_conflict",
        ) from error
    except ProjectVersionExhaustedError as error:
        raise ProblemException(
            status=409, title="Project version exhausted",
            detail="The project version cannot be advanced.",
            code="project_version_exhausted",
        ) from error
    except ProjectKeyConflictError as error:
        raise _project_key_conflict_problem() from error
    except ProjectDeleteBlockedError as error:
        raise _project_delete_blocked_problem(error) from error
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except (ProjectPermissionDeniedError, UserAdministrationDeniedError) as error:
        raise administrator_required_problem() from error
    except (
        ProjectNotFoundError, ProjectMemberUserNotFoundError, ProjectMemberNotFoundError,
    ) as error:
        raise project_not_found_problem() from error


def _project_response(project: StoredProject) -> ProjectResponse:
    """Project DTO を allowlist 方式の公開 response へ変換する。"""

    return ProjectResponse(
        project_id=project.project_id,
        key=project.key,
        name=project.name,
        description=project.description,
        status=project.status,
        settings=project.settings,
        retention_days=project.retention_days,
        row_version=project.row_version,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _member_response(member: StoredProjectMember) -> ProjectMemberResponse:
    """Membership DTO を credential のない公開 response へ変換する。"""

    return ProjectMemberResponse(
        user_id=member.user_id,
        email=member.email,
        display_name=member.display_name,
        status=member.status,
        joined_at=member.joined_at,
    )


def _project_key_conflict_problem() -> ProblemException:
    """Organization 内 key 重複を安定した 409 Problem へ変換する。"""

    return ProblemException(
        status=status.HTTP_409_CONFLICT,
        title="Project key conflict",
        detail="A project with the same key already exists.",
        code="project_key_conflict",
    )


def _project_delete_blocked_problem(error: ProjectDeleteBlockedError) -> ProblemException:
    """削除拒否を、利用者が次の操作を選べる安定 code 付き 409 へ変換する。

    Problem contract は追加 field を許さないため、阻害要因は code で区別する。Web は
    この code で archive 前、Run 履歴、Schedule 参照、所属監査による拒否を区別する。
    """

    code = "project_delete_requires_archive"
    if "run_history_exists" in error.blockers:
        code = "project_delete_blocked_by_runs"
    elif "task_schedule_exists" in error.blockers:
        code = "project_delete_blocked_by_schedules"
    elif "member_audit_exists" in error.blockers:
        code = "project_delete_blocked_by_member_audit"
    return ProblemException(
        status=status.HTTP_409_CONFLICT,
        title="Project delete rejected",
        detail=str(error),
        code=code,
    )
