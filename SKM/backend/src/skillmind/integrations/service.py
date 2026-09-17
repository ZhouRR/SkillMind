"""Integration と ResourceBinding の API/Worker transaction 境界を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.core.secret_crypto import SecretCipher, SecretCryptoError
from skillmind.integrations.domain import (
    CreateIntegrationCommand,
    CreateSecretReferenceCommand,
    IntegrationConflictError,
    PutResourceBindingCommand,
    ResolvedIntegration,
    ResolvedSecretReference,
    StoredIntegration,
    StoredResourceBinding,
    StoredSecretReference,
    UpdateSecretReferenceCommand,
)
from skillmind.integrations.repository import IntegrationRepository


class IntegrationService:
    """Project resource 管理の use case と transaction lifecycle を所有する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        secret_cipher: SecretCipher | None = None,
    ) -> None:
        """Database session factory と、MANAGED 封入に使う任意の KEK cipher を保持する。"""

        self._session_factory = session_factory
        self._secret_cipher = secret_cipher

    async def discover_mcp_tools(
        self, *, project_id: UUID, integration_id: UUID, expected_revision: int
    ) -> dict[str, Any]:
        """保存済み endpoint/credential だけで発見し、I/O 後にも原 revision を検査する。"""
        from skillmind.agent.mcp_tools_source import StreamableHttpMcpToolsSource
        from skillmind.integrations.domain import IntegrationStatus, IntegrationValidationError
        from skillmind.integrations.secrets import DeploymentSecretResolver

        resolver = DeploymentSecretResolver(cipher=self._secret_cipher)

        async def load() -> tuple[ResolvedIntegration, str | None]:
            """最新の有効な接続と秘密を一緒に照合する。"""
            item = await self.get_integration(project_id=project_id, integration_id=integration_id)
            if item.revision != expected_revision or item.status is not IntegrationStatus.ACTIVE:
                raise IntegrationConflictError("MCP connection changed; reload before discovery")
            if item.provider != "mcp":
                raise IntegrationValidationError("This connection is not MCP")
            token = None
            if item.secret_reference_id is not None:
                token = resolver.resolve(
                    await self.resolve_secret_reference(
                        project_id=project_id, secret_reference_id=item.secret_reference_id
                    )
                )
            return item, token

        original, token = await load()
        catalog = await StreamableHttpMcpToolsSource().discover(original.config, token)
        current, current_token = await load()
        if current != original or current_token != token:
            raise IntegrationConflictError("MCP connection changed during discovery")
        return catalog

    async def create_secret_reference(
        self, command: CreateSecretReferenceCommand
    ) -> StoredSecretReference:
        """SecretReference を一 transaction で作成する。MANAGED は密文を同時に保存する。"""

        async with self._session_factory() as session, session.begin():
            repository = IntegrationRepository(session, secret_cipher=self._secret_cipher)
            return await repository.create_secret_reference(command)

    async def update_secret_reference(
        self, command: UpdateSecretReferenceCommand
    ) -> StoredSecretReference:
        """認証情報の編集を暗号化と同じ transaction で確定する。"""

        try:
            async with self._session_factory() as session, session.begin():
                return await IntegrationRepository(
                    session, secret_cipher=self._secret_cipher
                ).update_secret_reference(command)
        except IntegrityError as error:
            raise IntegrationConflictError(
                "Resource changed concurrently; reload and review"
            ) from error

    async def delete_secret_reference(
        self, *, project_id: UUID, secret_reference_id: UUID, expected_updated_at: datetime
    ) -> None:
        """未参照の認証情報削除を確定し、並行参照は競合として返す。"""

        try:
            async with self._session_factory() as session, session.begin():
                await IntegrationRepository(session).delete_secret_reference(
                    project_id=project_id, secret_reference_id=secret_reference_id,
                    expected_updated_at=expected_updated_at,
                )
        except IntegrityError as error:
            raise IntegrationConflictError(
                "Resource is referenced or changed concurrently"
            ) from error

    async def get_integration_details(
        self, *, project_id: UUID, integration_id: UUID
    ) -> tuple[StoredIntegration, dict[str, Any]]:
        """ADMIN 編集用の非機密接続設定を metadata と同時に取得する。"""

        async with self._session_factory() as session:
            return await IntegrationRepository(session).get_integration_details(
                project_id=project_id, integration_id=integration_id,
            )

    async def update_integration(
        self, command: CreateIntegrationCommand, *, integration_id: UUID, expected_revision: int
    ) -> StoredIntegration:
        """接続先と権限を revision 付きで更新する。"""

        try:
            async with self._session_factory() as session, session.begin():
                return await IntegrationRepository(session).update_integration(
                    command, integration_id=integration_id, expected_revision=expected_revision,
                )
        except IntegrityError as error:
            raise IntegrationConflictError(
                "Resource changed concurrently; reload and review"
            ) from error

    async def delete_integration(
        self, *, project_id: UUID, integration_id: UUID, expected_revision: int
    ) -> None:
        """未参照接続の削除を確定し、FK 競合でも履歴を保護する。"""

        try:
            async with self._session_factory() as session, session.begin():
                await IntegrationRepository(session).delete_integration(
                    project_id=project_id, integration_id=integration_id,
                    expected_revision=expected_revision,
                )
        except IntegrityError as error:
            raise IntegrationConflictError(
                "Resource is referenced or changed concurrently"
            ) from error

    async def rotate_managed_secrets(self) -> tuple[int, int]:
        """全 MANAGED 密文を active KEK へ再封入し、(rotated, skipped) を返す。"""

        if self._secret_cipher is None:
            raise SecretCryptoError("Managed secret KEK is not configured")
        async with self._session_factory() as session, session.begin():
            repository = IntegrationRepository(session, secret_cipher=self._secret_cipher)
            return await repository.rotate_managed_material(self._secret_cipher)

    async def list_secret_references(
        self, *, project_id: UUID
    ) -> tuple[StoredSecretReference, ...]:
        """Project の SecretReference metadata を列挙する。"""

        async with self._session_factory() as session:
            return await IntegrationRepository(session).list_secret_references(
                project_id=project_id
            )

    async def disable_secret_reference(
        self, *, project_id: UUID, secret_reference_id: UUID
    ) -> StoredSecretReference:
        """SecretReference を新規利用不能にする。"""

        async with self._session_factory() as session, session.begin():
            return await IntegrationRepository(session).disable_secret_reference(
                project_id=project_id,
                secret_reference_id=secret_reference_id,
            )

    async def resolve_secret_reference(
        self, *, project_id: UUID, secret_reference_id: UUID
    ) -> ResolvedSecretReference:
        """Worker 用 locator metadata を所有確認付きで取得する。"""

        async with self._session_factory() as session:
            return await IntegrationRepository(session).resolve_secret_reference(
                project_id=project_id,
                secret_reference_id=secret_reference_id,
            )

    async def create_integration(
        self, command: CreateIntegrationCommand
    ) -> StoredIntegration:
        """登録済み Provider instance を作成する。"""

        async with self._session_factory() as session, session.begin():
            return await IntegrationRepository(session).create_integration(command)

    async def list_integrations(
        self, *, project_id: UUID, active_only: bool = False
    ) -> tuple[StoredIntegration, ...]:
        """Project の Integration を列挙する。"""

        async with self._session_factory() as session:
            return await IntegrationRepository(session).list_integrations(
                project_id=project_id,
                active_only=active_only,
            )

    async def get_integration(
        self, *, project_id: UUID, integration_id: UUID
    ) -> ResolvedIntegration:
        """Worker/内部 use case 用 Integration snapshot を返す。"""

        async with self._session_factory() as session:
            return await IntegrationRepository(session).get_integration(
                project_id=project_id,
                integration_id=integration_id,
            )

    async def disable_integration(
        self, *, project_id: UUID, integration_id: UUID, expected_revision: int
    ) -> StoredIntegration:
        """Optimistic revision を満たす Integration を無効化する。"""

        async with self._session_factory() as session, session.begin():
            return await IntegrationRepository(session).disable_integration(
                project_id=project_id,
                integration_id=integration_id,
                expected_revision=expected_revision,
            )

    async def put_resource_binding(
        self, command: PutResourceBindingCommand
    ) -> StoredResourceBinding:
        """Project default または Task override binding を保存する。"""

        async with self._session_factory() as session, session.begin():
            return await IntegrationRepository(session).put_resource_binding(command)

    async def list_resource_bindings(
        self, *, project_id: UUID
    ) -> tuple[StoredResourceBinding, ...]:
        """Project 内 binding と Run snapshot を列挙する。"""

        async with self._session_factory() as session:
            return await IntegrationRepository(session).list_resource_bindings(
                project_id=project_id
            )
