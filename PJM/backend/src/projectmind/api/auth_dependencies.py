"""Route 間で共有する session、CSRF、system role dependency。"""

from __future__ import annotations

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request, status

from projectmind.api.problems import ProblemException
from projectmind.auth.service import (
    AuthenticatedActor,
    AuthService,
    CsrfRejectedError,
    UnauthorizedSessionError,
)
from projectmind.core.settings import Settings
from projectmind.projects import ProjectNotFoundError, ProjectService, ProjectStatus, StoredProject
from projectmind.users.domain import UserAccess


def user_access(request: Request, actor: AuthenticatedActor) -> UserAccess:
    """入口 actor と原 credential を内部再認証へ渡し、公開 JSON と混ぜない。"""

    settings: Settings = request.app.state.settings
    return UserAccess(
        actor=actor,
        request_id=UUID(request.state.request_id),
        session_token=request.cookies.get(settings.auth_session_cookie_name, ""),
        csrf_token=request.headers.get("X-CSRF-Token", ""),
    )


async def authenticated_actor(request: Request) -> AuthenticatedActor:
    """Read request の opaque cookie を検証して actor を返す。"""

    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        return await service.authenticate_session(token)
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error


async def csrf_authenticated_actor(
    request: Request,
    x_csrf_token: str = Header(alias="X-CSRF-Token"),
) -> AuthenticatedActor:
    """Unsafe request の Origin、Session、CSRF を検証して actor を返す。"""

    require_same_origin(request)
    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        return await service.authenticate_unsafe_session(
            session_token=token,
            csrf_token=x_csrf_token,
        )
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error


async def admin_actor(
    actor: Annotated[AuthenticatedActor, Depends(csrf_authenticated_actor)],
) -> AuthenticatedActor:
    """System ADMIN だけを管理 use case へ通す。"""

    require_admin_role(actor)
    return actor


async def authenticated_admin_actor(
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
) -> AuthenticatedActor:
    """Safe read request を CSRF なしで system ADMIN に限定する。"""

    require_admin_role(actor)
    return actor


async def authenticated_project_actor(
    request: Request,
    project_id: UUID,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
) -> AuthenticatedActor:
    """Safe request の actor が Project を参照できることを検証する。"""

    await authorize_project_access(request, actor, project_id)
    return actor


async def csrf_project_actor(
    request: Request,
    project_id: UUID,
    actor: Annotated[AuthenticatedActor, Depends(csrf_authenticated_actor)],
) -> AuthenticatedActor:
    """Unsafe request の CSRF と Project access を同じ dependency chain で検証する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    return actor


async def authorize_project_access(
    request: Request,
    actor: AuthenticatedActor,
    project_id: UUID,
    *,
    require_active: bool = False,
) -> StoredProject:
    """存在と越権を同じ 404 に畳み、必要な mutation では archive を拒否する。"""

    service: ProjectService = request.app.state.project_service
    try:
        project = await service.get_project(actor=actor, project_id=project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    if require_active and project.status is not ProjectStatus.ACTIVE:
        raise project_archived_problem()
    return project


# Route module 間で共有する認証済み actor の dependency alias。
ReadActor = Annotated[AuthenticatedActor, Depends(authenticated_actor)]
WriteActor = Annotated[AuthenticatedActor, Depends(csrf_authenticated_actor)]
ProjectReadActor = Annotated[AuthenticatedActor, Depends(authenticated_project_actor)]
ProjectWriteActor = Annotated[AuthenticatedActor, Depends(csrf_project_actor)]
AdminReadActor = Annotated[AuthenticatedActor, Depends(authenticated_admin_actor)]
AdminWriteActor = Annotated[AuthenticatedActor, Depends(admin_actor)]


def require_admin_role(actor: AuthenticatedActor) -> None:
    """System role 不足を endpoint 間で同じ 403 Problem に変換する。"""

    if actor.system_role != "ADMIN":
        raise administrator_required_problem()


def administrator_required_problem() -> ProblemException:
    """管理操作の role 不足を共通の 403 Problem へ変換する。"""

    return ProblemException(
        status=status.HTTP_403_FORBIDDEN,
        title="Access denied",
        detail="Administrator access is required.",
        code="administrator_required",
    )


def project_not_found_problem() -> ProblemException:
    """Project の不存在と caller の非所属を区別しない 404 を生成する。"""

    return ProblemException(
        status=status.HTTP_404_NOT_FOUND,
        title="Project not found",
        detail="The requested project resource was not found.",
        code="project_not_found",
    )


def project_archived_problem() -> ProblemException:
    """入口検査と transaction 内の再検査で同じ帰档済み拒否を返す。"""

    return ProblemException(
        status=status.HTTP_409_CONFLICT,
        title="Project is archived",
        detail="Archived projects cannot be modified.",
        code="project_archived",
    )


def require_same_origin(request: Request) -> None:
    """Unsafe request の Origin が proxy 復元後の公開 origin と一致するか検証する。"""

    origin = request.headers.get("Origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin is None or not hmac.compare_digest(origin.rstrip("/"), expected.rstrip("/")):
        raise csrf_rejected_problem()


def authentication_required_problem() -> ProblemException:
    """Session failure を resource 存在情報なしの共通 401 へ畳み込む。"""

    return ProblemException(
        status=status.HTTP_401_UNAUTHORIZED,
        title="Authentication required",
        detail="A valid session is required.",
        code="authentication_required",
    )


def csrf_rejected_problem() -> ProblemException:
    """Origin と token の失敗を区別しない共通 403 を返す。"""

    return ProblemException(
        status=status.HTTP_403_FORBIDDEN,
        title="Request rejected",
        detail="The request origin or CSRF token was rejected.",
        code="csrf_rejected",
    )
