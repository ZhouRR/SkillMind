"""Project 作用域の module(SkillComposition)設定 API route を提供する。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from skillmind.api.auth_dependencies import (
    AdminWriteActor,
    ProjectReadActor,
    administrator_required_problem,
    authentication_required_problem,
    authorize_project_access,
    csrf_rejected_problem,
    project_archived_problem,
    project_not_found_problem,
    user_access,
)
from skillmind.api.problems import ProblemException, problem_openapi_response
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.compositions import (
    CompositionService,
    ModuleNotFoundError,
    ModuleSkillInvalidError,
    ModuleValidationError,
    StoredModule,
)
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.users.domain import UserAdministrationDeniedError

router = APIRouter()


class ModuleSkillResponse(BaseModel):
    """保存時に束縛した精確版の投影であり、現在も公開/有効であることは保証しない。"""

    skill_version_id: UUID
    skill_id: UUID
    skill_key: str
    skill_name: str
    version: str
    sort_order: int


class ModuleResponse(BaseModel):
    """Project で有効な module の公開 response。"""

    module_id: UUID
    project_id: UUID
    name: str
    description: str
    skills: list[ModuleSkillResponse]
    created_at: datetime
    updated_at: datetime


class ModuleListResponse(BaseModel):
    """Project 内 module の一覧。"""

    modules: list[ModuleResponse]


class ModuleWriteRequest(BaseModel):
    """Module 作成/更新の入力。束縛は PUBLISHED SkillVersion の ID 列。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    skill_version_ids: list[UUID] = Field(min_length=1, max_length=50)


@router.get(
    "/projects/{project_id}/modules",
    response_model=ModuleListResponse,
    tags=["modules"],
)
async def list_modules(
    request: Request,
    project_id: UUID,
    actor: ProjectReadActor,
) -> ModuleListResponse:
    """Project 成員に有効な module の一覧を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: CompositionService = request.app.state.composition_service
    modules = await service.list_modules(project_id=project_id)
    return ModuleListResponse(modules=[_module_response(module) for module in modules])


@router.post(
    "/projects/{project_id}/modules",
    response_model=ModuleResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Project not found or inaccessible"),
        409: problem_openapi_response("Project is archived"),
        422: problem_openapi_response("Request or module bindings failed validation"),
    },
    tags=["modules"],
)
async def create_module(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: ModuleWriteRequest,
) -> ModuleResponse:
    """ADMIN が module を作成し、現在 Project へ有効化する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: CompositionService = request.app.state.composition_service
    try:
        module = await service.create_module(
            access=user_access(request, actor),
            project_id=project_id,
            name=body.name,
            description=body.description,
            skill_version_ids=body.skill_version_ids,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except (ModuleValidationError, ModuleSkillInvalidError) as error:
        raise _module_rejected() from error
    return _module_response(module)


@router.put(
    "/projects/{project_id}/modules/{module_id}",
    response_model=ModuleResponse,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Module or Project not found or inaccessible"),
        409: problem_openapi_response("Project is archived"),
        422: problem_openapi_response("Request or module bindings failed validation"),
    },
    tags=["modules"],
)
async def update_module(
    request: Request,
    project_id: UUID,
    module_id: UUID,
    actor: AdminWriteActor,
    body: ModuleWriteRequest,
) -> ModuleResponse:
    """ADMIN が module の名称/説明/束縛集合を置き換える。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: CompositionService = request.app.state.composition_service
    try:
        module = await service.update_module(
            access=user_access(request, actor),
            project_id=project_id,
            module_id=module_id,
            name=body.name,
            description=body.description,
            skill_version_ids=body.skill_version_ids,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except ModuleNotFoundError as error:
        raise _module_not_found() from error
    except (ModuleValidationError, ModuleSkillInvalidError) as error:
        raise _module_rejected() from error
    return _module_response(module)


@router.delete(
    "/projects/{project_id}/modules/{module_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: problem_openapi_response("The original session is no longer valid"),
        403: problem_openapi_response("Administrator access or session CSRF was rejected"),
        404: problem_openapi_response("Module or Project not found or inaccessible"),
        409: problem_openapi_response("Project is archived"),
        422: problem_openapi_response("Request did not satisfy the API contract"),
    },
    tags=["modules"],
)
async def delete_module(
    request: Request,
    project_id: UUID,
    module_id: UUID,
    actor: AdminWriteActor,
) -> Response:
    """ADMIN が Project の module を削除する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: CompositionService = request.app.state.composition_service
    try:
        await service.delete_module(
            access=user_access(request, actor), project_id=project_id, module_id=module_id
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except ModuleNotFoundError as error:
        raise _module_not_found() from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _module_response(module: StoredModule) -> ModuleResponse:
    """Read model を公開 response へ変換する。"""

    return ModuleResponse(
        module_id=module.module_id,
        project_id=module.project_id,
        name=module.name,
        description=module.description,
        skills=[
            ModuleSkillResponse(
                skill_version_id=binding.skill_version_id,
                skill_id=binding.skill_id,
                skill_key=binding.skill_key,
                skill_name=binding.skill_name,
                version=binding.version,
                sort_order=binding.sort_order,
            )
            for binding in module.skills
        ],
        created_at=module.created_at,
        updated_at=module.updated_at,
    )


def _module_not_found() -> ProblemException:
    """Module の不存在/越権を安定した 404 Problem へ変換する。"""

    return ProblemException(
        status=404,
        title="Module not found",
        detail="The requested module resource was not found.",
        code="module_not_found",
    )


def _module_rejected() -> ProblemException:
    """名称・束縛の検証失敗を安定した 422 Problem へ変換する。"""

    return ProblemException(
        status=422,
        title="Module rejected",
        detail="The module name or skill bindings were rejected.",
        code="module_rejected",
    )
