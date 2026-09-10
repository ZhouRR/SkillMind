"""本人の安全操作、ADMIN のユーザー管理と既存 preference 資源を提供する。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from skillmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    ReadActor,
    WriteActor,
    administrator_required_problem,
    authentication_required_problem,
    csrf_rejected_problem,
    project_not_found_problem,
)
from skillmind.api.auth_dependencies import user_access as _user_access
from skillmind.api.login_protection import LOGIN_PROTECTION_RESPONSES, login_protection_problem
from skillmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from skillmind.auth.domain import UI_LANGUAGES
from skillmind.auth.login_protection import LoginProtectionUnavailableError, LoginRateLimitedError
from skillmind.auth.service import AuthService
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.core.settings import Settings
from skillmind.projects import ProjectNotFoundError, ProjectService, StoredProjectPreference
from skillmind.users.domain import (
    CreateUserCommand,
    CurrentPasswordRejectedError,
    LastActiveAdminError,
    StoredUser,
    StoredUserSecurityEvent,
    UpdateUserCommand,
    UserAdministrationDeniedError,
    UserEmailConflictError,
    UserMutationResult,
    UserNotFoundError,
    UserRole,
    UserSecurityAction,
    UserStatus,
    UserVersionConflictError,
)
from skillmind.users.service import UserService

# Preference は Project context の一部として公開済み OpenAPI の "projects" tag に凍結されている。
router = APIRouter(prefix="/users/me", tags=["projects"])

# UI 言語は Project に依存しない User 档案属性のため、独立 router で "users" tag に置く。
language_router = APIRouter(prefix="/users/me", tags=["users"])

# Account は credential と同じ cache 境界を持つ。旧 preference の tag は変更しない。
account_router = APIRouter(
    prefix="/users",
    tags=["users", "auth"],
    responses={
        401: problem_openapi_response(
            "A valid session is required.", headers=NO_STORE_PROBLEM_HEADERS
        ),
        403: problem_openapi_response(
            "Access or CSRF was rejected.", headers=NO_STORE_PROBLEM_HEADERS
        ),
        422: problem_openapi_response("Invalid account request.", headers=NO_STORE_PROBLEM_HEADERS),
    },
)
_CONFLICT = problem_openapi_response(
    "Account version, email uniqueness or last active administrator conflict.",
    headers=NO_STORE_PROBLEM_HEADERS,
)
_NOT_FOUND = problem_openapi_response(
    "User not found or inaccessible.", headers=NO_STORE_PROBLEM_HEADERS
)
# 成功も auth-tag middleware の cache 境界内にあり、拒否だけを宣言すると consumer が誤る。
_ACCOUNT_RESPONSE = {"headers": NO_STORE_PROBLEM_HEADERS}
_MUTATION_RESPONSE = {
    "headers": {
        **NO_STORE_PROBLEM_HEADERS,
        "Set-Cookie": {
            "description": "Deletes the current session cookie only when session_revoked is true.",
            "required": False,
            "schema": {"type": "string"},
        },
    }
}
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0)]


class UserAccountResponse(BaseModel):
    """内部 preference と password/hash を公開しない account allowlist。"""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    email: str
    display_name: str
    system_role: UserRole
    status: UserStatus
    row_version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class UserPageResponse(BaseModel):
    """組織内 server filter と paging の現在結果。"""

    model_config = ConfigDict(extra="forbid")

    items: list[UserAccountResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class UserSecurityEventResponse(BaseModel):
    """自由入力・credential・hash を含めない追加式の操作事実。"""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    user_id: UUID
    actor_id: UUID
    action: UserSecurityAction
    row_version: int = Field(ge=1)
    previous_role: UserRole | None
    previous_status: UserStatus | None
    system_role: UserRole
    status: UserStatus
    revoked_sessions: int = Field(ge=0)
    request_id: UUID
    created_at: datetime


class UserSecurityEventPageResponse(BaseModel):
    """一人の security event を全履歴の件数とともに page 化する。"""

    model_config = ConfigDict(extra="forbid")

    items: list[UserSecurityEventResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class UserMutationResponse(BaseModel):
    """現在の caller が再ログインすべきかを、撤銷行数と分けて返す。"""

    model_config = ConfigDict(extra="forbid")

    user: UserAccountResponse
    revoked_sessions: int = Field(ge=0)
    session_revoked: bool


class CreateUserRequest(BaseModel):
    """ADMIN が一度だけ提出する初期 password と明示した役割。"""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    system_role: UserRole
    password: SecretStr = Field(min_length=15, max_length=1024)


class UserVersionRequest(BaseModel):
    """表示中の account 版を必須にし、bool や文字列を版へ coercion しない。"""

    model_config = ConfigDict(extra="forbid")

    expected_row_version: int = Field(ge=1, strict=True)


class UpdateUserRequest(UserVersionRequest):
    """Login email を変更させない完全な account 編集。"""

    display_name: str = Field(min_length=1, max_length=200)
    system_role: UserRole
    status: UserStatus


class ChangeOwnPasswordRequest(UserVersionRequest):
    """現在と次の password は入力専用で repr にも露出させない。"""

    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=15, max_length=1024)


@account_router.get(
    "/me/account", response_model=UserAccountResponse, responses={200: _ACCOUNT_RESPONSE}
)
async def get_own_account(request: Request, actor: ReadActor) -> UserAccountResponse:
    """Project membership に依存せず現在の本人 account を読み出す。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        return _account(await service.get_account(access=_user_access(request, actor)))


@account_router.get(
    "/me/security-events",
    response_model=UserSecurityEventPageResponse,
    responses={200: _ACCOUNT_RESPONSE},
)
async def get_own_security_events(
    request: Request, actor: ReadActor, limit: PageLimit = 25, offset: PageOffset = 0
) -> UserSecurityEventPageResponse:
    """本人以外の ID を request から受け取らず、安全履歴を page 化する。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        events, total = await service.list_security_events(
            access=_user_access(request, actor), user_id=actor.user_id, limit=limit, offset=offset
        )
        return _event_page(events, total, limit, offset)


@account_router.post(
    "/me/password",
    response_model=UserMutationResponse,
    responses={
        200: _MUTATION_RESPONSE,
        **LOGIN_PROTECTION_RESPONSES,
        400: problem_openapi_response(
            "Current password was rejected.", headers=NO_STORE_PROBLEM_HEADERS
        ),
        409: _CONFLICT,
    },
)
async def change_own_password(
    request: Request, response: Response, body: ChangeOwnPasswordRequest, actor: WriteActor
) -> UserMutationResponse:
    """入口配額・Origin/Session CSRF の後で、本人改密と全旧会話の失効を行う。"""

    auth: AuthService = request.app.state.auth_service
    service: UserService = request.app.state.user_service
    with _user_errors():
        await auth.admit_password_change(actor=actor, admission=request.state.login_admission)
        result = await service.change_password(
            access=_user_access(request, actor),
            current_password=body.current_password.get_secret_value(),
            new_password=body.new_password.get_secret_value(),
            expected_row_version=body.expected_row_version,
        )
        return _mutation(request, response, result)


@account_router.post(
    "/me/sessions/revoke",
    response_model=UserMutationResponse,
    responses={200: _MUTATION_RESPONSE, 409: _CONFLICT},
)
async def revoke_own_sessions(
    request: Request, response: Response, body: UserVersionRequest, actor: WriteActor
) -> UserMutationResponse:
    """本人の全会話を失効させ、成功した current cookie だけを削除する。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        result = await service.revoke_sessions(
            access=_user_access(request, actor),
            user_id=actor.user_id,
            expected_row_version=body.expected_row_version,
        )
        return _mutation(request, response, result)


@account_router.get("", response_model=UserPageResponse, responses={200: _ACCOUNT_RESPONSE})
async def list_users(
    request: Request,
    actor: AdminReadActor,
    q: Annotated[str, Query(max_length=200)] = "",
    limit: PageLimit = 25,
    offset: PageOffset = 0,
) -> UserPageResponse:
    """ADMIN の組織で email/表示名を server filter し、全体件数と page を返す。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        users, total = await service.list_users(
            access=_user_access(request, actor), query=q, limit=limit, offset=offset
        )
        return UserPageResponse(
            items=[_account(user) for user in users], total=total, limit=limit, offset=offset
        )


@account_router.post(
    "",
    response_model=UserMutationResponse,
    status_code=201,
    responses={201: _MUTATION_RESPONSE, 409: _CONFLICT},
)
async def create_user(
    request: Request, response: Response, body: CreateUserRequest, actor: AdminWriteActor
) -> UserMutationResponse:
    """明示した役割の account を作成し、Project membership は自動追加しない。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        result = await service.create_user(
            access=_user_access(request, actor),
            command=CreateUserCommand(
                body.email, body.display_name, body.system_role, body.password.get_secret_value()
            ),
        )
        return _mutation(request, response, result)


@account_router.get(
    "/{user_id}",
    response_model=UserAccountResponse,
    responses={200: _ACCOUNT_RESPONSE, 404: _NOT_FOUND},
)
async def get_user_account(
    user_id: UUID, request: Request, actor: AdminReadActor
) -> UserAccountResponse:
    """組織内の精確 ID を読み、編集競合を一覧の再検索なしで確認する。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        return _account(
            await service.get_user(access=_user_access(request, actor), user_id=user_id)
        )


@account_router.put(
    "/{user_id}",
    response_model=UserMutationResponse,
    responses={200: _MUTATION_RESPONSE, 404: _NOT_FOUND, 409: _CONFLICT},
)
async def update_user(
    user_id: UUID,
    request: Request,
    response: Response,
    body: UpdateUserRequest,
    actor: AdminWriteActor,
) -> UserMutationResponse:
    """原版に対して資料/役割/状態を変更し、失効と監査を同一 use case に任せる。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        result = await service.update_user(
            access=_user_access(request, actor),
            user_id=user_id,
            command=UpdateUserCommand(
                body.display_name, body.system_role, body.status, body.expected_row_version
            ),
        )
        return _mutation(request, response, result)


@account_router.post(
    "/{user_id}/sessions/revoke",
    response_model=UserMutationResponse,
    responses={200: _MUTATION_RESPONSE, 404: _NOT_FOUND, 409: _CONFLICT},
)
async def revoke_user_sessions(
    user_id: UUID,
    request: Request,
    response: Response,
    body: UserVersionRequest,
    actor: AdminWriteActor,
) -> UserMutationResponse:
    """ADMIN が組織内の対象会話を持久失効させる。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        result = await service.revoke_sessions(
            access=_user_access(request, actor),
            user_id=user_id,
            expected_row_version=body.expected_row_version,
        )
        return _mutation(request, response, result)


@account_router.get(
    "/{user_id}/security-events",
    response_model=UserSecurityEventPageResponse,
    responses={200: _ACCOUNT_RESPONSE, 404: _NOT_FOUND},
)
async def get_user_security_events(
    user_id: UUID,
    request: Request,
    actor: AdminReadActor,
    limit: PageLimit = 25,
    offset: PageOffset = 0,
) -> UserSecurityEventPageResponse:
    """組織外の存在を漏らさず、一人の監査を ADMIN に返す。"""

    service: UserService = request.app.state.user_service
    with _user_errors():
        events, total = await service.list_security_events(
            access=_user_access(request, actor), user_id=user_id, limit=limit, offset=offset
        )
        return _event_page(events, total, limit, offset)


def _account(user: StoredUser) -> UserAccountResponse:
    """将来内部 DTO が増えても秘密が出ないよう、公開 field を列挙する。"""

    return UserAccountResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        system_role=user.system_role,
        status=user.status,
        row_version=user.row_version,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _event_page(
    events: tuple[StoredUserSecurityEvent, ...], total: int, limit: int, offset: int
) -> UserSecurityEventPageResponse:
    """自由 metadata を作らず、安全イベントの許可済み field だけを投影する。"""

    return UserSecurityEventPageResponse(
        items=[
            UserSecurityEventResponse(
                event_id=event.event_id,
                user_id=event.user_id,
                actor_id=event.actor_id,
                action=event.action,
                row_version=event.row_version,
                previous_role=event.previous_role,
                previous_status=event.previous_status,
                system_role=event.system_role,
                status=event.status,
                revoked_sessions=event.revoked_sessions,
                request_id=event.request_id,
                created_at=event.created_at,
            )
            for event in events
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def _mutation(
    request: Request, response: Response, result: UserMutationResult
) -> UserMutationResponse:
    """同じ管理 transaction で current session が失効した場合だけ cookie を消す。"""

    if result.session_revoked:
        settings: Settings = request.app.state.settings
        response.delete_cookie(
            settings.auth_session_cookie_name,
            path="/",
            secure=settings.auth_cookie_secure,
            httponly=True,
            samesite="strict",
        )
    return UserMutationResponse(
        user=_account(result.user),
        revoked_sessions=result.revoked_sessions,
        session_revoked=result.session_revoked,
    )


@contextmanager
def _user_errors() -> Iterator[None]:
    """管理 use case の拒否を一箇所で安定した Problem に変え、秘密を detail に含めない。"""

    try:
        yield
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except UserNotFoundError as error:
        raise ProblemException(
            status=404,
            title="User not found",
            detail="The requested user was not found.",
            code="user_not_found",
        ) from error
    except UserVersionConflictError as error:
        raise ProblemException(
            status=409,
            title="Account changed",
            detail="Refresh the account before making a new decision.",
            code="user_version_conflict",
        ) from error
    except UserEmailConflictError as error:
        raise ProblemException(
            status=409,
            title="Email already assigned",
            detail="The email is already assigned to a user.",
            code="user_email_conflict",
        ) from error
    except LastActiveAdminError as error:
        raise ProblemException(
            status=409,
            title="Administrator required",
            detail="The last active administrator must be retained.",
            code="last_active_admin",
        ) from error
    except CurrentPasswordRejectedError as error:
        raise ProblemException(
            status=400,
            title="Password rejected",
            detail="The current password was rejected.",
            code="current_password_rejected",
        ) from error
    except (LoginRateLimitedError, LoginProtectionUnavailableError) as error:
        raise login_protection_problem(error) from error
    except ValueError as error:
        raise ProblemException(
            status=422,
            title="Invalid account request",
            detail="The account request did not satisfy the validation rules.",
            code="invalid_user_request",
        ) from error


class ProjectPreferenceRequest(BaseModel):
    """User 自身が選択する nullable Project context。"""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID | None


class ProjectPreferenceResponse(BaseModel):
    """現在も actor が利用できる保存済み Project context。"""

    project_id: UUID | None


@router.get("/project-preference", response_model=ProjectPreferenceResponse)
async def get_project_preference(
    request: Request,
    actor: ReadActor,
) -> ProjectPreferenceResponse:
    """現在も認可済みの User Project preference を返す。"""

    service: ProjectService = request.app.state.project_service
    preference = await service.get_preference(actor=actor)
    return _preference_response(preference)


@router.put(
    "/project-preference",
    response_model=ProjectPreferenceResponse,
    responses={404: {"description": "Project not found or inaccessible"}},
)
async def set_project_preference(
    request: Request,
    body: ProjectPreferenceRequest,
    actor: WriteActor,
) -> ProjectPreferenceResponse:
    """CSRF と Project access を検証して User 自身の preference を更新する。"""

    service: ProjectService = request.app.state.project_service
    try:
        preference = await service.set_preference(actor=actor, project_id=body.project_id)
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    return _preference_response(preference)


def _preference_response(preference: StoredProjectPreference) -> ProjectPreferenceResponse:
    """Project preference DTO を nullable UUID だけの response へ変換する。"""

    return ProjectPreferenceResponse(project_id=preference.project_id)


class UiLanguageRequest(BaseModel):
    """User 自身が選択する nullable UI 言語(None は browser 追従へ戻す)。"""

    model_config = ConfigDict(extra="forbid")

    ui_language: Literal["zh", "ja", "en"] | None


class UiLanguageResponse(BaseModel):
    """保存済み UI 言語 preference(NULL は未設定=browser 追従)。"""

    ui_language: Literal["zh", "ja", "en"] | None


@language_router.get("/ui-language", response_model=UiLanguageResponse)
async def get_ui_language(
    request: Request,
    actor: ReadActor,
) -> UiLanguageResponse:
    """User 自身の保存済み UI 言語 preference を返す。"""

    service: AuthService = request.app.state.auth_service
    ui_language = await service.get_ui_language(actor=actor)
    return _ui_language_response(ui_language)


@language_router.put("/ui-language", response_model=UiLanguageResponse)
async def set_ui_language(
    request: Request,
    body: UiLanguageRequest,
    actor: WriteActor,
) -> UiLanguageResponse:
    """CSRF 検証後に User 自身の UI 言語 preference を更新する。"""

    service: AuthService = request.app.state.auth_service
    ui_language = await service.set_ui_language(actor=actor, ui_language=body.ui_language)
    return _ui_language_response(ui_language)


def _ui_language_response(ui_language: str | None) -> UiLanguageResponse:
    """保存値を Literal へ絞った公開 response に変換する。"""

    # DB check 制約と service の許可集合が値域を保証するため、cast は runtime 検査付きの
    # 型宣言に留まる。集合外の歴史値が現れた場合は未設定として扱い、error にしない。
    narrowed = cast(Literal["zh", "ja", "en"], ui_language) if ui_language in UI_LANGUAGES else None
    return UiLanguageResponse(ui_language=narrowed)
