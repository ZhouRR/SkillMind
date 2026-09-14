"""外部アプリ向けの組織全域 API key を管理する。秘密は発行応答だけに含める。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from skillmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    administrator_required_problem,
    authentication_required_problem,
    csrf_rejected_problem,
    user_access,
)
from skillmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from skillmind.auth.api_key_domain import ApiKeyNotFoundError, StoredApiKey
from skillmind.auth.api_keys import ApiKeyService
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.users.domain import UserAdministrationDeniedError

router = APIRouter(
    prefix="/api-keys",
    tags=["api-keys", "auth"],
    responses={
        code: problem_openapi_response(detail, headers=NO_STORE_PROBLEM_HEADERS)
        for code, detail in {
            401: "Authentication required.",
            403: "Access or CSRF rejected.",
            404: "API key not found.",
            422: "Invalid API key request.",
        }.items()
    },
)


class ApiKeyResponse(BaseModel):
    """名前と利用・失効時刻だけを投影し、資格 hash と秘密を含めない。"""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    key_prefix: str
    created_by: UUID
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyListResponse(BaseModel):
    """失効済みを含む組織内の管理一覧。"""

    model_config = ConfigDict(extra="forbid")
    items: list[ApiKeyResponse]


class CreateApiKeyRequest(BaseModel):
    """発行時に必要なのは識別名のみ。権限指定は受け付けない。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        """空白のみを拒否し、表示名を正規化する。"""
        if not value.strip():
            raise ValueError("A name is required")
        return value.strip()


class CreatedApiKeyResponse(BaseModel):
    """commit 済み key の秘密を一回だけ返す cache 禁止の発行応答。"""

    model_config = ConfigDict(extra="forbid")
    api_key: ApiKeyResponse
    token: str = Field(repr=False, pattern=r"^skm1\.[A-Za-z0-9_-]{43}$")


def response(record: StoredApiKey) -> ApiKeyResponse:
    """内部 DTO を明示した公開 allowlist に変換する。"""
    return ApiKeyResponse(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        created_by=record.created_by,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
    )


@contextmanager
def key_problems() -> Iterator[None]:
    """transaction 内の再認証拒否も入口と同じ Problem で返す。"""
    try:
        yield
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except UserAdministrationDeniedError as error:
        raise administrator_required_problem() from error
    except ApiKeyNotFoundError as error:
        raise ProblemException(
            status=404,
            title="API key not found",
            detail="The requested API key was not found.",
            code="api_key_not_found",
        ) from error


@router.get(
    "", response_model=ApiKeyListResponse, responses={200: {"headers": NO_STORE_PROBLEM_HEADERS}}
)
async def list_api_keys(request: Request, actor: AdminReadActor) -> ApiKeyListResponse:
    """現在の組織に属する key metadata だけを読む。"""
    service: ApiKeyService = request.app.state.api_key_service
    with key_problems():
        records = await service.list(user_access(request, actor))
    return ApiKeyListResponse(items=[response(record) for record in records])


@router.post(
    "",
    status_code=201,
    response_model=CreatedApiKeyResponse,
    responses={201: {"headers": NO_STORE_PROBLEM_HEADERS}},
)
async def create_api_key(
    body: CreateApiKeyRequest, request: Request, actor: AdminWriteActor
) -> CreatedApiKeyResponse:
    """新しい独立 key を発行する。応答喪失時の自動再送はしない。"""
    service: ApiKeyService = request.app.state.api_key_service
    with key_problems():
        created = await service.create(user_access(request, actor), body.name)
    return CreatedApiKeyResponse(api_key=response(created.record), token=created.token)


@router.post(
    "/{key_id}/revoke",
    response_model=ApiKeyResponse,
    responses={200: {"headers": NO_STORE_PROBLEM_HEADERS}},
)
async def revoke_api_key(key_id: UUID, request: Request, actor: AdminWriteActor) -> ApiKeyResponse:
    """精確 ID を永続失効させる。同じ key の再失効は no-op。"""
    service: ApiKeyService = request.app.state.api_key_service
    with key_problems():
        revoked = await service.revoke(user_access(request, actor), key_id)
    return response(revoked)
