"""認証 User 自身の /users/me preference 資源 endpoint を提供する。"""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from projectmind.api.auth_dependencies import ReadActor, WriteActor, project_not_found_problem
from projectmind.auth.domain import UI_LANGUAGES
from projectmind.auth.service import AuthService
from projectmind.projects import ProjectNotFoundError, ProjectService, StoredProjectPreference

# Preference は Project context の一部として公開済み OpenAPI の "projects" tag に凍結されている。
router = APIRouter(prefix="/users/me", tags=["projects"])

# UI 言語は Project に依存しない User 档案属性のため、独立 router で "users" tag に置く。
language_router = APIRouter(prefix="/users/me", tags=["users"])


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
    narrowed = (
        cast(Literal["zh", "ja", "en"], ui_language) if ui_language in UI_LANGUAGES else None
    )
    return UiLanguageResponse(ui_language=narrowed)
