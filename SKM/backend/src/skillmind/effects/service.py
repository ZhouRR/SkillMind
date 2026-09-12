"""Effect policy、claim、SecretReference と finalization の transaction 境界を提供する。"""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.run_binding import (
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.documents.library import DOCUMENT_WRITE_CAPABILITY, DocumentLibraryTarget
from skillmind.effects.domain import (
    ChangeProposalValidationError,
    ClaimedEffectExecution,
    CreatePreauthorizationCommand,
    EffectFailure,
    EffectLeaseValidationError,
    EffectProviderResult,
    StoredEffectExecution,
    StoredEffectPreauthorization,
)
from skillmind.effects.policy_repository import EffectPolicyRepository
from skillmind.effects.release import ExecutionFeatures
from skillmind.integrations.domain import ResolvedSecretReference
from skillmind.integrations.repository import IntegrationRepository
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.domain import lease_token_hash
from skillmind.runs.repository import RunRepository


class EffectService:
    """Controlled effect の API/Worker use case と database transaction を所有する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        execution_features: ExecutionFeatures | None = None,
        document_library_target: DocumentLibraryTarget | None = None,
    ) -> None:
        """Database session factory と段階実行を明示許可した capability 集合を保持する。"""

        self._session_factory = session_factory
        self._execution_features = execution_features or ExecutionFeatures(deferred=True)
        self._document_library_target = document_library_target

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
        async with self._session_factory() as session, session.begin():
            return await RunRepository(
                session, execution_features=self._execution_features,
                    document_library_target=self._document_library_target,
            ).claim_effect_execution(
                effect_execution_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_token_hash_value=lease_token_hash(lease_token),
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )

    async def heartbeat_effect_execution(
        self, claimed: ClaimedEffectExecution, *, provider_version: str, lease_seconds: int,
    ) -> datetime:
        """原 claim を共有認可 transaction で更新し、commit 後にだけ新期限を返す。"""

        async with self._session_factory() as session, session.begin():
            expires_at = await RunRepository(
                session, execution_features=self._execution_features,
                document_library_target=self._document_library_target,
            ).heartbeat_effect_execution(
                claimed, provider_version=provider_version, lease_seconds=lease_seconds,
            )
        return expires_at

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

    async def authorize_effect_step(
        self,
        claimed: ClaimedEffectExecution,
        credential: str | None,
        *,
        provider_version: str,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """登録済み段階認可だけを許し、同じ批准・binding・原凭据を外部 I/O 前に復験する。"""

        if not self._execution_features.effect_enabled(
            claimed.capability_version, claimed.operation
        ):
            raise PermissionError("Effect capability is not enabled for staged execution")
        try:
            async with self._session_factory() as session, session.begin():
                if claimed.capability_version == DOCUMENT_WRITE_CAPABILITY:
                    if claimed.integration_id is not None or credential is not None:
                        raise PermissionError("Document library has no Integration credential")
                else:
                    if claimed.integration_id is None or credential is None:
                        raise PermissionError("Integration credential is required")
                    bound = await load_bound_run_resource(
                        session,
                        project_id=claimed.project_id,
                        run_id=claimed.run_id,
                        binding_id=claimed.binding_id,
                        integration_id=claimed.integration_id,
                        provider=claimed.provider,
                        capability=claimed.capability_version,
                    )
                    current = await resolve_binding_secret(
                        session, resolver=secret_resolver,
                        integration=bound.integration, required=True,
                    )
                    if (
                        bound.integration.config != claimed.integration_config
                        or bound.integration.revision != claimed.integration_revision
                        or bound.integration.secret_reference_id != claimed.secret_reference_id
                        or bound.scope != claimed.integration_scope
                        or current is None
                        or not hmac.compare_digest(
                            current.encode("utf-8"), credential.encode("utf-8")
                        )
                    ):
                        raise PermissionError("Effect binding or credential changed after claim")
                # binding/Secret の読取待機も済ませてから原批准と lease の時計を検証する。
                await RunRepository(
                session, execution_features=self._execution_features,
                    document_library_target=self._document_library_target,
            ).authorize_effect_step(
                    claimed, provider_version=provider_version
                )
        except (
            EffectLeaseValidationError,
            ChangeProposalValidationError,
            RunBindingError,
            ProjectArchivedError,
            ProjectNotFoundError,
            UnauthorizedSessionError,
            LookupError,
        ) as error:
            raise PermissionError("Effect authority is unavailable") from error

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
            return await RunRepository(
                session, execution_features=self._execution_features,
                    document_library_target=self._document_library_target,
            ).finalize_effect_execution(
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
            return await RunRepository(
                session, execution_features=self._execution_features,
                    document_library_target=self._document_library_target,
            ).recover_expired_effects(
                now=datetime.now(UTC),
                limit=limit,
                max_attempts=max_attempts,
            )

    async def recover_expired_proposals(self, *, limit: int) -> int:
        """未回答の effect approval を期限切れにし、Run continuation を保存する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(
                session, execution_features=self._execution_features,
                    document_library_target=self._document_library_target,
            ).recover_expired_proposals(
                now=datetime.now(UTC),
                limit=limit,
            )
