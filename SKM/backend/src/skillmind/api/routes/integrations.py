"""ADMIN 向け Integration、SecretReference と ResourceBinding API を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from skillmind.api.auth_dependencies import (
    AdminReadActor,
    AdminWriteActor,
    authorize_project_access,
)
from skillmind.api.problems import ProblemException
from skillmind.core.secret_crypto import SecretCryptoError
from skillmind.integrations import (
    MANAGED_SECRET_LOCATOR,
    CreateIntegrationCommand,
    CreateSecretReferenceCommand,
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationService,
    IntegrationStatus,
    IntegrationValidationError,
    PutResourceBindingCommand,
    ResourceBindingLevel,
    ResourceBindingNotFoundError,
    SecretReferenceNotFoundError,
    SecretResolver,
    StoredIntegration,
    StoredResourceBinding,
    StoredSecretReference,
)

router = APIRouter()


class CreateSecretReferenceRequest(BaseModel):
    """SecretReference 作成 request。

    ``locator`` は ENVIRONMENT/FILE 用。``secret_value`` は MANAGED 用の一過性明文で、
    サーバが即座に KEK 封入し、平文のまま保存・返却・ログ化しない。両者は resolver ごとに
    排他で、endpoint が組合せを検証する。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=64)
    resolver: SecretResolver
    locator: str | None = Field(default=None, min_length=1, max_length=512)
    key_version: str = Field(min_length=1, max_length=128)
    secret_value: str | None = Field(default=None, min_length=1, max_length=8192)


class SecretReferenceResponse(BaseModel):
    """Locator と Secret 本文を除外した SecretReference response。"""

    secret_reference_id: UUID
    project_id: UUID
    name: str
    provider: str
    resolver: SecretResolver
    key_version: str
    status: IntegrationStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


class SecretReferenceListResponse(BaseModel):
    """Project の SecretReference metadata 一覧。"""

    items: list[SecretReferenceResponse]


class CreateIntegrationRequest(BaseModel):
    """登録済み Provider instance を作成する request。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    capabilities: list[str] = Field(min_length=1, max_length=20)
    scope: dict[str, Any]
    config: dict[str, Any]
    secret_reference_id: UUID | None = None


class IntegrationResponse(BaseModel):
    """接続設定値と Secret locator を除外した Integration response。"""

    integration_id: UUID
    project_id: UUID
    name: str
    kind: str
    provider: str
    status: IntegrationStatus
    revision: int
    capabilities: list[str]
    scope: dict[str, Any]
    config_keys: list[str]
    secret_reference_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


class IntegrationListResponse(BaseModel):
    """Project の Integration 一覧。"""

    items: list[IntegrationResponse]


class DisableIntegrationRequest(BaseModel):
    """Optimistic revision 付き Integration disable request。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class PutResourceBindingRequest(BaseModel):
    """Project default または Task override の binding request。"""

    model_config = ConfigDict(extra="forbid")

    scope_level: ResourceBindingLevel
    scope_key: str = Field(min_length=1, max_length=256)
    requirement_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$"
    )
    resource_kind: str = Field(min_length=1, max_length=64)
    integration_id: UUID
    capability_version: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*/[vV][0-9][a-zA-Z0-9_.-]*$",
    )
    requested_scope: dict[str, Any]


class ResourceBindingResponse(BaseModel):
    """Permission-sensitive snapshot を明示列挙した ResourceBinding response。"""

    binding_id: UUID
    project_id: UUID
    scope_level: ResourceBindingLevel
    scope_key: str
    requirement_key: str
    resource_kind: str
    integration_id: UUID | None
    run_id: UUID | None
    source_binding_id: UUID | None
    provider: str
    capability_version: str
    revision: str
    scope: dict[str, Any]
    checksum: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


class ResourceBindingListResponse(BaseModel):
    """Project 内 ResourceBinding 一覧。"""

    items: list[ResourceBindingResponse]


@router.post(
    "/projects/{project_id}/secret-references",
    response_model=SecretReferenceResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["integrations"],
)
async def create_secret_reference(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: CreateSecretReferenceRequest,
) -> SecretReferenceResponse:
    """ADMIN が locator(ENVIRONMENT/FILE)または明文(MANAGED)で SecretReference を登録する。

    MANAGED の明文はサーバが即座に KEK 封入し、平文のまま保存・返却・ログ化しない。
    """

    await authorize_project_access(request, actor, project_id, require_active=True)
    # resolver ごとに locator/secret_value の組合せを固定する。MANAGED は密文を DB に持ち
    # 外部 locator を持たない(sentinel 固定)。ENVIRONMENT/FILE は locator 必須で明文不可。
    if body.resolver is SecretResolver.MANAGED:
        if body.locator is not None:
            raise _validation_problem(
                IntegrationValidationError("Managed SecretReference must not carry a locator")
            )
        locator = MANAGED_SECRET_LOCATOR
    else:
        if body.locator is None:
            raise _validation_problem(
                IntegrationValidationError("SecretReference requires a locator")
            )
        locator = body.locator
    service: IntegrationService = request.app.state.integration_service
    try:
        stored = await service.create_secret_reference(
            CreateSecretReferenceCommand(
                project_id=project_id,
                name=body.name,
                provider=body.provider,
                resolver=body.resolver,
                locator=locator,
                key_version=body.key_version,
                created_by=actor.user_id,
                secret_value=body.secret_value,
            )
        )
    except SecretCryptoError as error:
        # KEK 未設定・不正は request の不備ではなく配備側の問題。500 で traceback を晒さず、
        # 運用者が原因を判別できる安定 code の 503 として返す。
        raise _managed_secret_unavailable(error) from error
    except IntegrationValidationError as error:
        raise _validation_problem(error) from error
    except IntegrationConflictError as error:
        raise _conflict_problem(error) from error
    return _secret_response(stored)


@router.get(
    "/projects/{project_id}/secret-references",
    response_model=SecretReferenceListResponse,
    tags=["integrations"],
)
async def list_secret_references(
    request: Request,
    project_id: UUID,
    actor: AdminReadActor,
) -> SecretReferenceListResponse:
    """ADMIN に locator を含まない SecretReference metadata を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: IntegrationService = request.app.state.integration_service
    items = await service.list_secret_references(project_id=project_id)
    return SecretReferenceListResponse(items=[_secret_response(item) for item in items])


@router.post(
    "/projects/{project_id}/secret-references/{secret_reference_id}/disable",
    response_model=SecretReferenceResponse,
    tags=["integrations"],
)
async def disable_secret_reference(
    request: Request,
    project_id: UUID,
    secret_reference_id: UUID,
    actor: AdminWriteActor,
) -> SecretReferenceResponse:
    """ADMIN が SecretReference を物理削除せず無効化する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: IntegrationService = request.app.state.integration_service
    try:
        stored = await service.disable_secret_reference(
            project_id=project_id,
            secret_reference_id=secret_reference_id,
        )
    except SecretReferenceNotFoundError as error:
        raise _not_found_problem("SecretReference", error) from error
    return _secret_response(stored)


@router.post(
    "/projects/{project_id}/integrations",
    response_model=IntegrationResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["integrations"],
)
async def create_integration(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: CreateIntegrationRequest,
) -> IntegrationResponse:
    """ADMIN が登録済み Provider の Project instance を作成する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: IntegrationService = request.app.state.integration_service
    try:
        stored = await service.create_integration(
            CreateIntegrationCommand(
                project_id=project_id,
                name=body.name,
                kind=body.kind,
                provider=body.provider,
                capabilities=tuple(body.capabilities),
                scope=body.scope,
                config=body.config,
                secret_reference_id=body.secret_reference_id,
                created_by=actor.user_id,
            )
        )
    except IntegrationValidationError as error:
        raise _validation_problem(error) from error
    except IntegrationConflictError as error:
        raise _conflict_problem(error) from error
    except SecretReferenceNotFoundError as error:
        raise _not_found_problem("SecretReference", error) from error
    return _integration_response(stored)


@router.get(
    "/projects/{project_id}/integrations",
    response_model=IntegrationListResponse,
    tags=["integrations"],
)
async def list_integrations(
    request: Request,
    project_id: UUID,
    actor: AdminReadActor,
) -> IntegrationListResponse:
    """ADMIN に Project の Integration metadata を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: IntegrationService = request.app.state.integration_service
    items = await service.list_integrations(project_id=project_id)
    return IntegrationListResponse(items=[_integration_response(item) for item in items])


@router.post(
    "/projects/{project_id}/integrations/{integration_id}/disable",
    response_model=IntegrationResponse,
    tags=["integrations"],
)
async def disable_integration(
    request: Request,
    project_id: UUID,
    integration_id: UUID,
    actor: AdminWriteActor,
    body: DisableIntegrationRequest,
) -> IntegrationResponse:
    """ADMIN が optimistic revision を指定して Integration を無効化する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: IntegrationService = request.app.state.integration_service
    try:
        stored = await service.disable_integration(
            project_id=project_id,
            integration_id=integration_id,
            expected_revision=body.expected_revision,
        )
    except IntegrationNotFoundError as error:
        raise _not_found_problem("Integration", error) from error
    except IntegrationConflictError as error:
        raise _conflict_problem(error) from error
    return _integration_response(stored)


@router.put(
    "/projects/{project_id}/resource-bindings",
    response_model=ResourceBindingResponse,
    tags=["integrations"],
)
async def put_resource_binding(
    request: Request,
    project_id: UUID,
    actor: AdminWriteActor,
    body: PutResourceBindingRequest,
) -> ResourceBindingResponse:
    """ADMIN が Project default または Task override binding を保存する。"""

    await authorize_project_access(request, actor, project_id, require_active=True)
    service: IntegrationService = request.app.state.integration_service
    try:
        stored = await service.put_resource_binding(
            PutResourceBindingCommand(
                project_id=project_id,
                scope_level=body.scope_level,
                scope_key=body.scope_key,
                requirement_key=body.requirement_key,
                resource_kind=body.resource_kind,
                integration_id=body.integration_id,
                capability_version=body.capability_version,
                requested_scope=body.requested_scope,
                created_by=actor.user_id,
            )
        )
    except (IntegrationValidationError, ResourceBindingNotFoundError) as error:
        raise _validation_problem(error) from error
    except IntegrationNotFoundError as error:
        raise _not_found_problem("Integration", error) from error
    return _binding_response(stored)


@router.get(
    "/projects/{project_id}/resource-bindings",
    response_model=ResourceBindingListResponse,
    tags=["integrations"],
)
async def list_resource_bindings(
    request: Request,
    project_id: UUID,
    actor: AdminReadActor,
) -> ResourceBindingListResponse:
    """ADMIN に Project の binding と immutable Run snapshot を返す。"""

    await authorize_project_access(request, actor, project_id)
    service: IntegrationService = request.app.state.integration_service
    items = await service.list_resource_bindings(project_id=project_id)
    return ResourceBindingListResponse(items=[_binding_response(item) for item in items])


def _secret_response(value: StoredSecretReference) -> SecretReferenceResponse:
    """SecretReference read model を公開 allowlist response へ変換する。"""

    return SecretReferenceResponse(
        secret_reference_id=value.secret_reference_id,
        project_id=value.project_id,
        name=value.name,
        provider=value.provider,
        resolver=value.resolver,
        key_version=value.key_version,
        status=value.status,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        disabled_at=value.disabled_at,
    )


def _integration_response(value: StoredIntegration) -> IntegrationResponse:
    """Integration read model を接続情報なしの公開 response へ変換する。"""

    return IntegrationResponse(
        integration_id=value.integration_id,
        project_id=value.project_id,
        name=value.name,
        kind=value.kind,
        provider=value.provider,
        status=value.status,
        revision=value.revision,
        capabilities=list(value.capabilities),
        scope=value.scope,
        config_keys=list(value.config_keys),
        secret_reference_id=value.secret_reference_id,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        disabled_at=value.disabled_at,
    )


def _binding_response(value: StoredResourceBinding) -> ResourceBindingResponse:
    """ResourceBinding read model を公開 response へ変換する。"""

    return ResourceBindingResponse(
        binding_id=value.binding_id,
        project_id=value.project_id,
        scope_level=value.scope_level,
        scope_key=value.scope_key,
        requirement_key=value.requirement_key,
        resource_kind=value.resource_kind,
        integration_id=value.integration_id,
        run_id=value.run_id,
        source_binding_id=value.source_binding_id,
        provider=value.provider,
        capability_version=value.capability_version,
        revision=value.revision,
        scope=value.scope,
        checksum=value.checksum,
        created_by=value.created_by,
        created_at=value.created_at,
        updated_at=value.updated_at,
        disabled_at=value.disabled_at,
    )


def _validation_problem(error: Exception) -> ProblemException:
    """Resource 設定違反を値を反射しない 422 Problem へ変換する。"""

    return ProblemException(
        status=422,
        title="Integration configuration rejected",
        detail=str(error),
        code="integration_configuration_invalid",
    )


def _conflict_problem(error: Exception) -> ProblemException:
    """Revision/name 競合を安定した 409 Problem へ変換する。"""

    return ProblemException(
        status=409,
        title="Integration conflict",
        detail=str(error),
        code="integration_conflict",
    )


def _managed_secret_unavailable(error: Exception) -> ProblemException:
    """KEK 未配線・不正を安定した 503 Problem へ変換する。

    SecretCryptoError の message は core/secret_crypto.py が鍵材料を反射しない語彙へ
    限定済みのため、そのまま detail へ載せて運用者の原因判別を助ける。
    """

    return ProblemException(
        status=503,
        title="Managed secret storage unavailable",
        detail=str(error),
        code="managed_secret_unavailable",
    )


def _not_found_problem(resource: str, error: Exception) -> ProblemException:
    """Project ownership 不一致と不存在を同じ 404 へ畳む。"""

    return ProblemException(
        status=404,
        title=f"{resource} not found",
        detail=str(error),
        code="integration_resource_not_found",
    )
