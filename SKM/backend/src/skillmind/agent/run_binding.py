"""Run に凍結された ResourceBinding を Provider 境界で再検証する単一実装。

Redmine 読取と repository 読取/物化は、いずれも「Run 作成後に Integration や binding が
すり替えられていないか」を実行直前に確かめる必要がある。同じ判定を Provider ごとに書き写すと
片方だけ緩む余地が生まれるため、checksum・status・provider・revision・capability の再検証を
ここへ一本化する。凭据本文はここでは扱わず、解決だけを別関数で行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import ResourceBinding
from skillmind.integrations.domain import (
    IntegrationStatus,
    ResolvedIntegration,
    ResourceBindingLevel,
    binding_checksum,
)
from skillmind.integrations.repository import IntegrationRepository
from skillmind.integrations.secrets import DeploymentSecretResolver, SecretResolutionError


class RunBindingError(RuntimeError):
    """Binding 再検証の失敗を、凭据を含まない安定 code で表す。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        """Agent へ返してよい code と message だけを保持する。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BoundRunResource:
    """再検証済みの Integration と、Run に凍結された scope・checksum。"""

    integration: ResolvedIntegration
    scope: dict[str, Any]
    # 物化 manifest へ来歴として書き出す監査値 (docs/06 §6.4)。秘密は含まない。
    checksum: str


async def load_bound_run_resource(
    session: AsyncSession,
    *,
    project_id: UUID,
    run_id: UUID,
    binding_id: UUID,
    integration_id: UUID,
    provider: str,
    capability: str,
) -> BoundRunResource:
    """Run 層 binding と Integration を突き合わせ、凍結内容との一致を確認する。

    不一致 (checksum 改竄、Integration の無効化、revision 変更、capability 剥奪) は
    すべて実行前に fail closed とする。Run 作成時点の権限上限を超えないための最終関門。
    """

    repository = IntegrationRepository(session)
    try:
        integration = await repository.get_integration(
            project_id=project_id, integration_id=integration_id
        )
    except LookupError as error:
        raise RunBindingError(
            "not_found", "Integration was not found", retryable=False
        ) from error
    binding = (
        await session.scalars(
            select(ResourceBinding).where(
                ResourceBinding.id == binding_id,
                ResourceBinding.run_id == run_id,
                ResourceBinding.project_id == project_id,
                ResourceBinding.scope_level == ResourceBindingLevel.RUN.value,
                ResourceBinding.integration_id == integration_id,
            )
        )
    ).one_or_none()
    if binding is None:
        raise RunBindingError(
            "invalid_request", "Run has no resource binding for this Tool", retryable=False
        )
    expected_checksum = binding_checksum(
        project_id=binding.project_id,
        scope_level=ResourceBindingLevel.RUN,
        scope_key=binding.scope_key,
        requirement_key=binding.requirement_key,
        resource_kind=binding.resource_kind,
        integration_id=binding.integration_id,
        provider=binding.provider,
        capability_version=binding.capability_version,
        revision=binding.revision,
        scope=dict(binding.scope_json),
    )
    if (
        integration.status is not IntegrationStatus.ACTIVE
        or integration.provider != provider
        or binding.provider != provider
        or integration.revision != int(binding.revision)
        or binding.checksum != expected_checksum
        or capability not in integration.capabilities
    ):
        raise RunBindingError(
            "unavailable", "Resource binding changed after Run creation", retryable=False
        )
    return BoundRunResource(
        integration=integration,
        scope=dict(binding.scope_json),
        checksum=binding.checksum,
    )


async def resolve_binding_secret(
    session: AsyncSession,
    *,
    resolver: DeploymentSecretResolver,
    integration: ResolvedIntegration,
    required: bool,
) -> str | None:
    """Integration の SecretReference を Worker 内でだけ解決する。

    ``required`` が False の Provider (匿名 clone 可能な git など) では未設定を許すが、
    設定されていれば必ず解決する。解決失敗の理由は反射せず unavailable へ畳む。
    """

    if integration.secret_reference_id is None:
        if required:
            raise RunBindingError(
                "unavailable", "Integration credential is not configured", retryable=False
            )
        return None
    try:
        reference = await IntegrationRepository(session).resolve_secret_reference(
            project_id=integration.project_id,
            secret_reference_id=integration.secret_reference_id,
        )
        return resolver.resolve(reference)
    except (LookupError, SecretResolutionError) as error:
        raise RunBindingError(
            "unavailable", "Integration credential is unavailable", retryable=False
        ) from error
