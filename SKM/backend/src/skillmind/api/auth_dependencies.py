"""Route 間で共有する session、CSRF、system role dependency。"""

from __future__ import annotations

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from skillmind.api.problems import ProblemException
from skillmind.auth.api_keys import ApiKeyService
from skillmind.auth.domain import derive_api_key_proof
from skillmind.auth.service import (
    AuthenticatedActor,
    AuthService,
    CsrfRejectedError,
    UnauthorizedSessionError,
)
from skillmind.core.settings import Settings
from skillmind.projects import ProjectNotFoundError, ProjectService, ProjectStatus, StoredProject
from skillmind.users.domain import UserAccess


def user_access(request: Request, actor: AuthenticatedActor) -> UserAccess:
    """入口 actor と原 credential を内部再認証へ渡し、公開 JSON と混ぜない。"""

    settings: Settings = request.app.state.settings
    return UserAccess(
        actor=actor,
        request_id=UUID(request.state.request_id),
        session_token=getattr(request.state, "api_key_token", None)
        or request.cookies.get(settings.auth_session_cookie_name, ""),
        csrf_token=(
            derive_api_key_proof(request.state.api_key_token)
            if getattr(request.state, "api_key_token", None)
            else request.headers.get("X-CSRF-Token", "")
        ),
    )


# OpenAPI には両方式を示すが、曖昧な header の判定は raw header で一元化する。
_bearer = HTTPBearer(auto_error=False, scheme_name="ApiKeyBearer")
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ApiKeyHeader")


async def api_key_actor(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    key_header: Annotated[str | None, Depends(_key_header)],
) -> AuthenticatedActor | None:
    """明示資格がある場合は cookie へ fallback せず、重複・混在も拒否する。"""
    headers = request.headers.getlist("authorization")
    keys = request.headers.getlist("x-api-key")
    if not headers and not keys:
        return None
    if len(headers) + len(keys) != 1:
        raise authentication_required_problem()
    if headers:
        parts = headers[0].split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise authentication_required_problem()
        token = parts[1]
    else:
        token = keys[0]
    service: ApiKeyService = request.app.state.api_key_service
    try:
        actor, key_id = await service.authenticate(token)
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    request.state.api_key_token = token
    request.state.api_key_id = key_id
    return actor


async def authenticated_actor(
    request: Request,
    key_actor: Annotated[AuthenticatedActor | None, Depends(api_key_actor)],
) -> AuthenticatedActor:
    """Read request の header key または opaque cookie を検証する。"""
    if key_actor is not None:
        return key_actor
    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        return await service.authenticate_session(token)
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error


async def csrf_authenticated_actor(
    request: Request,
    key_actor: Annotated[AuthenticatedActor | None, Depends(api_key_actor)],
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> AuthenticatedActor:
    """Header key は検証済み資格、cookie は従来の Origin/CSRF で書込を認証する。"""
    if key_actor is not None:
        return key_actor
    if x_csrf_token is None:
        # Cookie consumer の既存 422 契約を維持し、key request だけを例外にする。
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("header", "X-CSRF-Token"),
                    "msg": "Field required",
                    "input": None,
                }
            ]
        )
    require_same_origin(request)
    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        return await service.authenticate_unsafe_session(
            session_token=token,
            csrf_token=x_csrf_token or "",
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
        detail="A valid session or API key is required.",
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
