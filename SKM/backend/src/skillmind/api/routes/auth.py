"""Browser login、session refresh、logout の公開 API を提供する。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from skillmind.api.auth_dependencies import (
    authentication_required_problem,
    csrf_rejected_problem,
    require_same_origin,
)
from skillmind.api.login_protection import LOGIN_PROTECTION_RESPONSES, login_protection_problem
from skillmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from skillmind.auth.login_protection import LoginProtectionUnavailableError, LoginRateLimitedError
from skillmind.auth.service import (
    AuthService,
    CsrfRejectedError,
    InvalidCredentialsError,
    LoginCsrfError,
    LoginResult,
    SessionResult,
    UnauthorizedSessionError,
)
from skillmind.core.settings import Settings

router = APIRouter(prefix="/auth", tags=["auth"])
LOGIN_CSRF_COOKIE = "skillmind_login_csrf"

# Framework 既定の validation JSON ではなく、実際の Problem handler と同期する。
_CSRF_RESPONSE = problem_openapi_response(
    "Origin or CSRF verification was rejected.", headers=NO_STORE_PROBLEM_HEADERS
)
_VALIDATION_RESPONSE = problem_openapi_response(
    "The body or required request headers are invalid.", headers=NO_STORE_PROBLEM_HEADERS
)
_AUTHENTICATION_RESPONSE = problem_openapi_response(
    "A valid session is required.", headers=NO_STORE_PROBLEM_HEADERS
)


class LoginContextResponse(BaseModel):
    """Login form が memory 上だけで保持する短期 CSRF challenge。"""

    csrf_token: str
    expires_in_seconds: int


class LoginRequest(BaseModel):
    """Password login の明示 field だけを許可する request。"""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class AuthenticatedUserResponse(BaseModel):
    """Password/session hash を除外した認証済み user。"""

    user_id: UUID
    organization_id: UUID
    email: str
    display_name: str
    system_role: str


class SessionResponse(BaseModel):
    """Web shell が利用する current user と CSRF token。"""

    user: AuthenticatedUserResponse
    csrf_token: str
    absolute_expires_at: datetime
    deferred_features_enabled: bool = False


@router.get(
    "/login-context", response_model=LoginContextResponse, responses=LOGIN_PROTECTION_RESPONSES
)
async def login_context(request: Request, response: Response) -> LoginContextResponse:
    """一回限りの login CSRF challenge を発行する。"""

    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    try:
        token = await service.issue_login_csrf(request.state.login_admission)
    except LoginProtectionUnavailableError as error:
        raise login_protection_problem(error) from error
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        token,
        max_age=settings.auth_login_csrf_ttl_seconds,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return LoginContextResponse(
        csrf_token=token,
        expires_in_seconds=settings.auth_login_csrf_ttl_seconds,
    )


@router.post(
    "/login",
    response_model=SessionResponse,
    responses={
        **LOGIN_PROTECTION_RESPONSES,
        401: problem_openapi_response(
            "Invalid credentials; account existence is not disclosed.",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        403: _CSRF_RESPONSE,
        422: _VALIDATION_RESPONSE,
    },
)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    x_csrf_token: str = Header(alias="X-CSRF-Token"),
) -> SessionResponse:
    """Origin/CSRF/password を検証し、opaque Session cookie を設定する。"""

    require_same_origin(request)
    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    try:
        result = await service.login(
            email=body.email,
            password=body.password,
            login_csrf_header=x_csrf_token,
            login_csrf_cookie=request.cookies.get(LOGIN_CSRF_COOKIE, ""),
            client_address=request.client.host if request.client is not None else "unknown",
            admission=request.state.login_admission,
        )
    except LoginCsrfError as error:
        raise csrf_rejected_problem() from error
    except InvalidCredentialsError as error:
        raise ProblemException(
            status=status.HTTP_401_UNAUTHORIZED,
            title="Authentication failed",
            detail="Invalid email or password.",
            code="invalid_credentials",
        ) from error
    except (LoginRateLimitedError, LoginProtectionUnavailableError) as error:
        raise login_protection_problem(error) from error

    response.set_cookie(
        settings.auth_session_cookie_name,
        result.session_token,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
    return _session_response(result, settings.deferred_features_enabled)


@router.get("/session", response_model=SessionResponse, responses={401: _AUTHENTICATION_RESPONSE})
async def current_session(request: Request) -> SessionResponse:
    """有効 session actor と安定 CSRF token を返し、他ページを失効させない。"""

    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        result = await service.get_session(token)
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    return _session_response(result, settings.deferred_features_enabled)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: _AUTHENTICATION_RESPONSE, 403: _CSRF_RESPONSE, 422: _VALIDATION_RESPONSE},
)
async def logout(
    request: Request,
    response: Response,
    x_csrf_token: str = Header(alias="X-CSRF-Token"),
) -> None:
    """Origin/CSRF 検証後に server session と Cookie を失効させる。"""

    require_same_origin(request)
    service: AuthService = request.app.state.auth_service
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.auth_session_cookie_name, "")
    try:
        await service.logout(session_token=token, csrf_token=x_csrf_token)
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    response.delete_cookie(settings.auth_session_cookie_name, path="/")


def _session_response(
    result: LoginResult | SessionResult, deferred_features_enabled: bool,
) -> SessionResponse:
    """Domain result を field allowlist の公開 response へ変換する。"""

    return SessionResponse(
        user=AuthenticatedUserResponse(
            user_id=result.actor.user_id,
            organization_id=result.actor.organization_id,
            email=result.actor.email,
            display_name=result.actor.display_name,
            system_role=result.actor.system_role,
        ),
        csrf_token=result.csrf_token,
        absolute_expires_at=result.absolute_expires_at,
        deferred_features_enabled=deferred_features_enabled,
    )
