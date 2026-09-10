"""Effect policy、claim、SecretReference と finalization の transaction 境界を提供する。"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.effects.domain import (
    ClaimedEffectExecution,
    CreatePreauthorizationCommand,
    EffectFailure,
    EffectProviderResult,
    StoredEffectExecution,
    StoredEffectPreauthorization,
)
from skillmind.effects.policy_repository import EffectPolicyRepository
from skillmind.integrations.domain import ResolvedSecretReference
from skillmind.integrations.repository import IntegrationRepository
from skillmind.runs.domain import lease_token_hash
from skillmind.runs.repository import RunRepository


class EffectService:
    """Controlled effect の API/Worker use case と database transaction を所有する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def create_preauthorization(
        self, command: CreatePreauthorizationCommand
    ) -> StoredEffectPreauthorization:
        """LOW-only exact scope policy を作成する。"""

        async with self._session_factory() as session, session.begin():
            return await EffectPolicyRepository(session).create(command)

    async def list_preauthorizations(
        self, *, project_id: UUID
    ) -> tuple[StoredEffectPreauthorization, ...]:
        """Project の effect policies を列挙する。"""

        async with self._session_factory() as session:
            return await EffectPolicyRepository(session).list_for_project(
                project_id=project_id
            )

    async def disable_preauthorization(
        self,
        *,
        project_id: UUID,
        preauthorization_id: UUID,
        expected_policy_version: int,
    ) -> StoredEffectPreauthorization:
        """Optimistic policy version を満たす policy を無効化する。"""

        async with self._session_factory() as session, session.begin():
            return await EffectPolicyRepository(session).disable(
                project_id=project_id,
                preauthorization_id=preauthorization_id,
                expected_policy_version=expected_policy_version,
            )

    async def claim_effect_execution(
        self,
        effect_execution_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedEffectExecution | None:
        """Raw token を返し、database には hash だけを保存して effect を claim する。"""

        lease_token = secrets.token_urlsafe(32)
        lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).claim_effect_execution(
                effect_execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_token_hash_value=lease_token_hash(lease_token),
                lease_expires_at=lease_expires_at,
                max_attempts=max_attempts,
            )

    async def resolve_secret_reference(
        self, claimed: ClaimedEffectExecution
    ) -> ResolvedSecretReference | None:
        """Claimed Integration の SecretReference locator metadata を ownership 付きで返す。"""

        if claimed.secret_reference_id is None:
            return None
        async with self._session_factory() as session:
            return await IntegrationRepository(session).resolve_secret_reference(
                project_id=claimed.project_id,
                secret_reference_id=claimed.secret_reference_id,
            )

    async def finalize_effect_execution(
        self,
        claimed: ClaimedEffectExecution,
        *,
        result: EffectProviderResult | None,
        failure: EffectFailure | None,
        duration_ms: int,
        trace_id: str | None = None,
    ) -> StoredEffectExecution:
        """Provider outcome と Run continuation を同じ transaction で確定する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).finalize_effect_execution(
                claimed,
                result=result,
                failure=failure,
                duration_ms=duration_ms,
                trace_id=trace_id,
            )

    async def recover_expired_effects(
        self, *, limit: int, max_attempts: int
    ) -> int:
        """期限切れ effect lease を fail-closed に回収し、必要な再 dispatch を保存する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).recover_expired_effects(
                now=datetime.now(UTC),
                limit=limit,
                max_attempts=max_attempts,
            )

    async def recover_expired_proposals(self, *, limit: int) -> int:
        """未回答の effect approval を期限切れにし、Run continuation を保存する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).recover_expired_proposals(
                now=datetime.now(UTC),
                limit=limit,
            )
