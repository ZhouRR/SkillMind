"""認証済み Project CRUD と membership 管理 API を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from projectmind.api.auth_dependencies import (
    ReadActor,
    WriteActor,
    administrator_required_problem,
    project_not_found_problem,
)
from projectmind.api.problems import ProblemException, problem_openapi_response
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
    StoredProject,
    StoredProjectMember,
    UpdateProjectCommand,
)

router = APIRouter()


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

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    settings: dict[str, Any] | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def require_change(self) -> UpdateProjectRequest:
        """空 PATCH を拒否し、監査上意味のない updated_at 変更を防ぐ。"""

        if not self.model_fields_set:
            raise ValueError("At least one project field must be provided")
        return self


class ProjectResponse(BaseModel):
    """内部 organization field を含まない Project response。"""

    project_id: UUID
    key: str
    name: str
    description: str
    status: ProjectStatus
    settings: dict[str, Any]
    retention_days: int
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """Actor が参照可能な Project 一覧 response。"""

    items: list[ProjectResponse]


class ProjectMemberResponse(BaseModel):
    """Credential を含まない Project membership response。"""

    user_id: UUID
    email: str
    display_name: str
    status: ProjectMemberStatus
    joined_at: datetime


class ProjectMemberListResponse(BaseModel):
    """Project membership 一覧 response。"""

    items: list[ProjectMemberResponse]


@router.get("/projects", response_model=ProjectListResponse, tags=["projects"])
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
    responses={403: {"description": "ADMIN required"}, 409: {"description": "Key conflict"}},
    tags=["projects"],
)
async def create_project(
    request: Request,
    actor: WriteActor,
    body: CreateProjectRequest,
) -> ProjectResponse:
    """ADMIN の Organization に新規 Project を作成する。"""

    service: ProjectService = request.app.state.project_service
    try:
        project = await service.create_project(
            actor=actor,
            key=body.key,
            name=body.name,
            description=body.description,
            settings=body.settings,
            retention_days=body.retention_days,
        )
    except ProjectKeyConflictError as error:
        raise _project_key_conflict_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return _project_response(project)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    responses={
        401: problem_openapi_response("A valid session is required"),
        404: problem_openapi_response("Project not found or inaccessible"),
        422: problem_openapi_response("Invalid Project identity"),
    },
    tags=["projects"],
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
    responses={403: {"description": "ADMIN required"}, 404: {"description": "Project not found"}},
    tags=["projects"],
)
async def update_project(
    request: Request,
    project_id: UUID,
    actor: WriteActor,
    body: UpdateProjectRequest,
) -> ProjectResponse:
    """ADMIN が Project の変更可能な metadata を更新する。"""

    service: ProjectService = request.app.state.project_service
    try:
        project = await service.update_project(
            actor=actor,
            project_id=project_id,
            command=UpdateProjectCommand(
                name=body.name,
                description=body.description,
                settings=body.settings,
                retention_days=body.retention_days,
            ),
        )
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return _project_response(project)


@router.post(
    "/projects/{project_id}/archive",
    response_model=ProjectResponse,
    responses={403: {"description": "ADMIN required"}, 404: {"description": "Project not found"}},
    tags=["projects"],
)
async def archive_project(
    request: Request,
    project_id: UUID,
    actor: WriteActor,
) -> ProjectResponse:
    """ADMIN が Project を物理削除せず ARCHIVED にする。"""

    service: ProjectService = request.app.state.project_service
    try:
        project = await service.archive_project(actor=actor, project_id=project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return _project_response(project)


@router.post(
    "/projects/{project_id}/unarchive",
    response_model=ProjectResponse,
    responses={403: {"description": "ADMIN required"}, 404: {"description": "Project not found"}},
    tags=["projects"],
)
async def unarchive_project(
    request: Request,
    project_id: UUID,
    actor: WriteActor,
) -> ProjectResponse:
    """ADMIN が ARCHIVED Project を ACTIVE へ戻す。

    key は archive しても解放しないため、同じ key の作成が 409 になった利用者の復旧経路は
    新規作成ではなくこの復元になる。
    """

    service: ProjectService = request.app.state.project_service
    try:
        project = await service.unarchive_project(actor=actor, project_id=project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return _project_response(project)


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: problem_openapi_response("A valid session is required"),
        403: problem_openapi_response("ADMIN, same Origin and CSRF required"),
        404: problem_openapi_response("Project not found or inaccessible"),
        409: problem_openapi_response(
            "Deletion rejected: project_delete_requires_archive, "
            "project_delete_blocked_by_runs, or project_delete_blocked_by_schedules"
        ),
        422: problem_openapi_response("Invalid Project identity or missing CSRF header"),
    },
    tags=["projects"],
)
async def delete_project(
    request: Request,
    project_id: UUID,
    actor: WriteActor,
) -> Response:
    """ADMIN が Run/Schedule のない ARCHIVED Project を物理削除し key を解放する。"""

    service: ProjectService = request.app.state.project_service
    try:
        await service.delete_project(actor=actor, project_id=project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    except ProjectDeleteBlockedError as error:
        raise _project_delete_blocked_problem(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/members",
    response_model=ProjectMemberListResponse,
    responses={403: {"description": "ADMIN required"}, 404: {"description": "Project not found"}},
    tags=["projects"],
)
async def list_project_members(
    request: Request,
    project_id: UUID,
    actor: ReadActor,
) -> ProjectMemberListResponse:
    """ADMIN に Project membership の一覧を返す。"""

    service: ProjectService = request.app.state.project_service
    try:
        members = await service.list_members(actor=actor, project_id=project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return ProjectMemberListResponse(items=[_member_response(member) for member in members])


@router.put(
    "/projects/{project_id}/members/{user_id}",
    response_model=ProjectMemberResponse,
    responses={
        403: {"description": "ADMIN required"},
        404: {"description": "Project or active User not found"},
    },
    tags=["projects"],
)
async def add_project_member(
    request: Request,
    project_id: UUID,
    user_id: UUID,
    actor: WriteActor,
) -> ProjectMemberResponse:
    """ADMIN が同一 Organization の ACTIVE User を Project に追加する。"""

    service: ProjectService = request.app.state.project_service
    try:
        member = await service.add_member(actor=actor, project_id=project_id, user_id=user_id)
    except (ProjectNotFoundError, ProjectMemberUserNotFoundError) as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return _member_response(member)


@router.delete(
    "/projects/{project_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "ADMIN required"},
        404: {"description": "Project or active membership not found"},
    },
    tags=["projects"],
)
async def remove_project_member(
    request: Request,
    project_id: UUID,
    user_id: UUID,
    actor: WriteActor,
) -> Response:
    """ADMIN が membership を物理削除せず REMOVED にする。"""

    service: ProjectService = request.app.state.project_service
    try:
        await service.remove_member(actor=actor, project_id=project_id, user_id=user_id)
    except (ProjectNotFoundError, ProjectMemberNotFoundError) as error:
        raise project_not_found_problem() from error
    except ProjectPermissionDeniedError as error:
        raise administrator_required_problem() from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    この code で archive 前、Run 履歴、Schedule 参照による拒否を区別する。
    """

    code = "project_delete_requires_archive"
    if "run_history_exists" in error.blockers:
        code = "project_delete_blocked_by_runs"
    elif "task_schedule_exists" in error.blockers:
        code = "project_delete_blocked_by_schedules"
    return ProblemException(
        status=status.HTTP_409_CONFLICT,
        title="Project delete rejected",
        detail=str(error),
        code=code,
    )
