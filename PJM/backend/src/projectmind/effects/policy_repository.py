"""Effect preauthorization policy の管理と exact-scope 照合を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import EffectPreauthorization
from projectmind.effects.catalog import resolve_effect_capability
from projectmind.effects.domain import (
    ChangeProposalValidationError,
    CreatePreauthorizationCommand,
    EffectPreauthorizationNotFoundError,
    EffectRiskLevel,
    PreauthorizationStatus,
    StoredEffectPreauthorization,
)
from projectmind.integrations.domain import (
    IntegrationStatus,
    IntegrationValidationError,
    ensure_explicit_scope,
    normalize_provider_scope,
    scope_is_subset,
)
from projectmind.integrations.repository import IntegrationRepository


class EffectPolicyRepository:
    """Transaction-scoped session 上で低 risk preauthorization を操作する。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def create(
        self, command: CreatePreauthorizationCommand
    ) -> StoredEffectPreauthorization:
        """Active Integration の非空 exact scope に LOW-only policy を作成する。"""

        integration = await IntegrationRepository(self._session).get_integration(
            project_id=command.project_id,
            integration_id=command.integration_id,
        )
        if integration.status is not IntegrationStatus.ACTIVE:
            raise IntegrationValidationError("Disabled Integration cannot be preauthorized")
        if command.capability_version not in integration.capabilities:
            raise IntegrationValidationError(
                "Preauthorization capability is not provided by the Integration"
            )
        try:
            capability = resolve_effect_capability(command.capability_version)
        except ChangeProposalValidationError as error:
            raise IntegrationValidationError(
                "Effect capability is not eligible for preauthorization"
            ) from error
        if not capability.preauthorizable or integration.provider not in capability.providers:
            # 事前許可は「承認なしで apply してよい」という宣言。repository.write のように
            # 常時人手承認を要する capability は、policy を作れること自体が抜け穴になる。
            raise IntegrationValidationError(
                "Effect capability is not eligible for preauthorization"
            )
        scope = normalize_provider_scope(integration.provider, command.scope, write_enabled=True)
        # 事前許可は承認なし apply の境界のため、Integration 側が wildcard でも
        # policy 自体は逐項列挙だけを受け付ける(docs/06 の explicit 事前許可)。
        ensure_explicit_scope(scope)
        if not scope_is_subset(requested=scope, allowed=integration.scope):
            raise IntegrationValidationError("Preauthorization scope exceeds Integration scope")
        now = datetime.now(UTC)
        if command.expires_at is not None and command.expires_at <= now:
            raise IntegrationValidationError("Preauthorization expiry must be in the future")
        row = EffectPreauthorization(
            id=uuid4(),
            project_id=command.project_id,
            integration_id=command.integration_id,
            capability_version=command.capability_version,
            operation=command.operation,
            max_risk_level=EffectRiskLevel.LOW.value,
            scope_json=scope,
            status=PreauthorizationStatus.ACTIVE.value,
            policy_version=1,
            created_by=command.created_by,
            expires_at=command.expires_at,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        return _stored(row)

    async def list_for_project(
        self, *, project_id: UUID
    ) -> tuple[StoredEffectPreauthorization, ...]:
        """Project の active/disabled policy を安定順で列挙する。"""

        rows = await self._session.scalars(
            select(EffectPreauthorization)
            .where(EffectPreauthorization.project_id == project_id)
            .order_by(EffectPreauthorization.created_at, EffectPreauthorization.id)
        )
        return tuple(_stored(row) for row in rows)

    async def disable(
        self,
        *,
        project_id: UUID,
        preauthorization_id: UUID,
        expected_policy_version: int,
    ) -> StoredEffectPreauthorization:
        """Optimistic policy version を満たす policy を無効化する。"""

        row = (
            await self._session.scalars(
                select(EffectPreauthorization)
                .where(
                    EffectPreauthorization.id == preauthorization_id,
                    EffectPreauthorization.project_id == project_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise EffectPreauthorizationNotFoundError("Preauthorization not found in project")
        if row.policy_version != expected_policy_version:
            raise ValueError("Preauthorization policy version is stale")
        if row.status == PreauthorizationStatus.ACTIVE.value:
            now = datetime.now(UTC)
            row.status = PreauthorizationStatus.DISABLED.value
            row.policy_version += 1
            row.disabled_at = now
            row.updated_at = now
        return _stored(row)

    async def match(
        self,
        *,
        project_id: UUID,
        integration_id: UUID,
        capability_version: str,
        operation: str,
        risk_level: EffectRiskLevel,
        requested_scope: dict[str, Any],
        now: datetime,
    ) -> StoredEffectPreauthorization | None:
        """LOW risk と全 exact dimensions を満たす最初の active policy を返す。"""

        if risk_level is not EffectRiskLevel.LOW:
            return None
        rows = await self._session.scalars(
            select(EffectPreauthorization)
            .where(
                EffectPreauthorization.project_id == project_id,
                EffectPreauthorization.integration_id == integration_id,
                EffectPreauthorization.capability_version == capability_version,
                EffectPreauthorization.operation == operation,
                EffectPreauthorization.status == PreauthorizationStatus.ACTIVE.value,
            )
            .order_by(EffectPreauthorization.created_at, EffectPreauthorization.id)
        )
        normalized_requested = {
            key: list(value) if isinstance(value, list) else []
            for key, value in requested_scope.items()
        }
        for row in rows:
            if row.expires_at is not None and row.expires_at <= now:
                continue
            if scope_is_subset(
                requested=normalized_requested,
                allowed=dict(row.scope_json),
            ):
                return _stored(row)
        return None


def _stored(row: EffectPreauthorization) -> StoredEffectPreauthorization:
    """ORM row を公開 policy read model へ変換する。"""

    return StoredEffectPreauthorization(
        preauthorization_id=row.id,
        project_id=row.project_id,
        integration_id=row.integration_id,
        capability_version=row.capability_version,
        operation=row.operation,
        max_risk_level=EffectRiskLevel(row.max_risk_level),
        scope=dict(row.scope_json),
        status=PreauthorizationStatus(row.status),
        policy_version=row.policy_version,
        created_by=row.created_by,
        expires_at=row.expires_at,
        disabled_at=row.disabled_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
