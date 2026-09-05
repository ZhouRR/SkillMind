"""Integration、SecretReference と ResourceBinding の PostgreSQL 永続化を実装する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.core.secret_crypto import (
    EncryptedSecret,
    SecretCipher,
    SecretCryptoError,
    managed_secret_aad,
)
from projectmind.db.models import (
    Integration,
    ManagedSecretMaterial,
    ResourceBinding,
    SecretReference,
)
from projectmind.integrations.domain import (
    REGISTERED_WRITE_CAPABILITIES,
    CreateIntegrationCommand,
    CreateSecretReferenceCommand,
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationStatus,
    IntegrationValidationError,
    PutResourceBindingCommand,
    ResolvedIntegration,
    ResolvedRunBinding,
    ResolvedSecretReference,
    ResourceBindingLevel,
    ResourceBindingNotFoundError,
    SecretReferenceNotFoundError,
    SecretResolver,
    StoredIntegration,
    StoredResourceBinding,
    StoredSecretReference,
    binding_checksum,
    normalize_integration_command,
    normalize_provider_scope,
    scope_is_subset,
    validate_secret_reference,
)


class IntegrationRepository:
    """Transaction-scoped session 上で Project resource aggregate を操作する。"""

    def __init__(
        self, session: AsyncSession, *, secret_cipher: SecretCipher | None = None
    ) -> None:
        """Transaction-scoped session と、MANAGED 封入に使う任意の KEK cipher を保持する。"""

        self._session = session
        self._secret_cipher = secret_cipher

    async def create_secret_reference(
        self, command: CreateSecretReferenceCommand
    ) -> StoredSecretReference:
        """検証済み locator metadata を保存し、locator を含まない read model を返す。"""

        validate_secret_reference(command)
        existing = (
            await self._session.scalars(
                select(SecretReference).where(
                    SecretReference.project_id == command.project_id,
                    SecretReference.name == command.name.strip(),
                )
            )
        ).one_or_none()
        if existing is not None:
            raise IntegrationConflictError("SecretReference name is already in use")
        now = datetime.now(UTC)
        reference_id = uuid4()
        row = SecretReference(
            id=reference_id,
            project_id=command.project_id,
            name=command.name.strip(),
            provider=command.provider,
            resolver=command.resolver.value,
            locator=command.locator,
            key_version=command.key_version,
            status=IntegrationStatus.ACTIVE.value,
            created_by=command.created_by,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        if command.resolver is SecretResolver.MANAGED:
            # 密文行は SecretReference を FK 参照するが、両者を結ぶ relationship を意図的に
            # 持たない(密文を read model へ lazy load させないため)。relationship が無いと
            # unit of work は mapper 間の INSERT 順序を保証せず、実 DB では子行が先に
            # 挿入されて FK 違反になる。親行をここで確定させて順序を固定する。
            await self._session.flush()
            self._store_managed_material(command, reference_id=reference_id, now=now)
        return _stored_secret(row)

    def _store_managed_material(
        self,
        command: CreateSecretReferenceCommand,
        *,
        reference_id: UUID,
        now: datetime,
    ) -> None:
        """明文を即座に KEK 封入し、密文専用 table に 1:1 で保存する。明文は保持しない。"""

        if self._secret_cipher is None:
            # KEK 未設定での MANAGED 作成は fail closed。暗号化なしで明文相当を持たせない。
            # これは request の不備ではなく配備側の設定不足のため、422 ではなく 503 へ写る
            # SecretCryptoError とする(service.rotate_managed_secrets と同じ扱い)。
            raise SecretCryptoError("Managed secret storage is not configured")
        if command.secret_value is None:
            # validate_secret_reference で保証済みだが、型と防御のため明示的に確認する。
            raise IntegrationValidationError("Managed SecretReference requires a secret value")
        aad = managed_secret_aad(
            project_id=command.project_id, secret_reference_id=reference_id
        )
        encrypted = self._secret_cipher.encrypt(command.secret_value, aad=aad)
        self._session.add(
            ManagedSecretMaterial(
                id=uuid4(),
                secret_reference_id=reference_id,
                project_id=command.project_id,
                kek_version=encrypted.kek_version,
                nonce=encrypted.nonce,
                ciphertext=encrypted.ciphertext,
                created_at=now,
                updated_at=now,
            )
        )

    async def list_secret_references(
        self, *, project_id: UUID
    ) -> tuple[StoredSecretReference, ...]:
        """Project 内の SecretReference metadata を locator なしで列挙する。"""

        rows = await self._session.scalars(
            select(SecretReference)
            .where(SecretReference.project_id == project_id)
            .order_by(SecretReference.name, SecretReference.id)
        )
        return tuple(_stored_secret(row) for row in rows)

    async def resolve_secret_reference(
        self, *, project_id: UUID, secret_reference_id: UUID
    ) -> ResolvedSecretReference:
        """Worker 用に所有確認済み locator metadata を返す。"""

        row = await self._session.get(SecretReference, secret_reference_id)
        if row is None or row.project_id != project_id:
            raise SecretReferenceNotFoundError("SecretReference not found in project")
        material: EncryptedSecret | None = None
        if row.resolver == SecretResolver.MANAGED.value:
            material_row = (
                await self._session.scalars(
                    select(ManagedSecretMaterial).where(
                        ManagedSecretMaterial.secret_reference_id == secret_reference_id
                    )
                )
            ).one_or_none()
            if material_row is None:
                # MANAGED は作成時に必ず密文行を伴う。欠落はデータ不整合として fail closed。
                raise SecretReferenceNotFoundError("Managed secret material not found")
            material = EncryptedSecret(
                kek_version=material_row.kek_version,
                nonce=material_row.nonce,
                ciphertext=material_row.ciphertext,
            )
        return _resolved_secret(row, material)

    async def disable_secret_reference(
        self, *, project_id: UUID, secret_reference_id: UUID
    ) -> StoredSecretReference:
        """SecretReference を追加利用不能にし、既存監査参照は保持する。"""

        row = await self._session.get(SecretReference, secret_reference_id, with_for_update=True)
        if row is None or row.project_id != project_id:
            raise SecretReferenceNotFoundError("SecretReference not found in project")
        if row.status == IntegrationStatus.ACTIVE.value:
            now = datetime.now(UTC)
            row.status = IntegrationStatus.DISABLED.value
            row.disabled_at = now
            row.updated_at = now
        return _stored_secret(row)

    async def rotate_managed_material(self, cipher: SecretCipher) -> tuple[int, int]:
        """全 MANAGED 密文を active KEK へ再封入し、(rotated, skipped) を返す。

        行ごとの ``kek_version`` で復号鍵を選び、AAD は保存済み project/reference から再構成する。
        既に active version の行は skip する。復号不能(旧鍵欠落/改竄)は例外で fail closed。
        """

        rotated = 0
        skipped = 0
        rows = (
            await self._session.scalars(
                select(ManagedSecretMaterial).with_for_update()
            )
        ).all()
        for row in rows:
            if row.kek_version == cipher.active_version:
                skipped += 1
                continue
            aad = managed_secret_aad(
                project_id=row.project_id, secret_reference_id=row.secret_reference_id
            )
            # 平文はこの loop 内の一過性 local のみ。ログ化・永続化・返却しない。
            plaintext = cipher.decrypt(
                EncryptedSecret(
                    kek_version=row.kek_version,
                    nonce=row.nonce,
                    ciphertext=row.ciphertext,
                ),
                aad=aad,
            )
            reencrypted = cipher.encrypt(plaintext, aad=aad)
            row.kek_version = reencrypted.kek_version
            row.nonce = reencrypted.nonce
            row.ciphertext = reencrypted.ciphertext
            row.updated_at = datetime.now(UTC)
            rotated += 1
        return rotated, skipped

    async def create_integration(
        self, command: CreateIntegrationCommand
    ) -> StoredIntegration:
        """Provider catalog と SecretReference を検証して Integration revision 1 を作成する。"""

        normalized = normalize_integration_command(command)
        existing = (
            await self._session.scalars(
                select(Integration).where(
                    Integration.project_id == normalized.project_id,
                    Integration.name == normalized.name,
                )
            )
        ).one_or_none()
        if existing is not None:
            raise IntegrationConflictError("Integration name is already in use")
        if normalized.secret_reference_id is not None:
            secret = await self.resolve_secret_reference(
                project_id=normalized.project_id,
                secret_reference_id=normalized.secret_reference_id,
            )
            if (
                secret.status is not IntegrationStatus.ACTIVE
                or secret.provider != normalized.provider
            ):
                raise IntegrationValidationError(
                    "Integration SecretReference is inactive or belongs to another Provider"
                )
        now = datetime.now(UTC)
        row = Integration(
            id=uuid4(),
            project_id=normalized.project_id,
            name=normalized.name,
            kind=normalized.kind,
            provider=normalized.provider,
            status=IntegrationStatus.ACTIVE.value,
            revision=1,
            capabilities_json=list(normalized.capabilities),
            scope_json=normalized.scope,
            config_json=normalized.config,
            secret_reference_id=normalized.secret_reference_id,
            created_by=normalized.created_by,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        return _stored_integration(row)

    async def list_integrations(
        self, *, project_id: UUID, active_only: bool = False
    ) -> tuple[StoredIntegration, ...]:
        """Project 内 Integration を安定順で列挙する。"""

        statement = select(Integration).where(Integration.project_id == project_id)
        if active_only:
            statement = statement.where(Integration.status == IntegrationStatus.ACTIVE.value)
        rows = await self._session.scalars(statement.order_by(Integration.name, Integration.id))
        return tuple(_stored_integration(row) for row in rows)

    async def get_integration(
        self, *, project_id: UUID, integration_id: UUID, for_update: bool = False
    ) -> ResolvedIntegration:
        """Project ownership を確認して config 付き Integration を内部層へ返す。"""

        statement = select(Integration).where(
            Integration.id == integration_id, Integration.project_id == project_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.scalars(statement)).one_or_none()
        if row is None:
            raise IntegrationNotFoundError("Integration not found in project")
        return _resolved_integration(row)

    async def disable_integration(
        self, *, project_id: UUID, integration_id: UUID, expected_revision: int
    ) -> StoredIntegration:
        """Revision を検査して Integration を fail-closed に無効化する。"""

        row = (
            await self._session.scalars(
                select(Integration)
                .where(Integration.id == integration_id, Integration.project_id == project_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise IntegrationNotFoundError("Integration not found in project")
        if row.revision != expected_revision:
            raise IntegrationConflictError("Integration revision is stale")
        if row.status == IntegrationStatus.ACTIVE.value:
            now = datetime.now(UTC)
            row.status = IntegrationStatus.DISABLED.value
            row.revision += 1
            row.disabled_at = now
            row.updated_at = now
        return _stored_integration(row)

    async def put_resource_binding(
        self, command: PutResourceBindingCommand
    ) -> StoredResourceBinding:
        """Project/Task 層の binding を Integration scope 内で作成または置換する。"""

        if command.scope_level is ResourceBindingLevel.RUN:
            raise IntegrationValidationError("Run bindings can only be frozen by Run creation")
        _validate_binding_identity(command)
        integration = await self.get_integration(
            project_id=command.project_id,
            integration_id=command.integration_id,
        )
        if integration.status is not IntegrationStatus.ACTIVE:
            raise IntegrationValidationError("Disabled Integration cannot be bound")
        if integration.kind != command.resource_kind:
            raise IntegrationValidationError("Integration kind does not satisfy the requirement")
        if command.capability_version not in integration.capabilities:
            raise IntegrationValidationError(
                "Integration does not provide the requested capability"
            )
        requested_scope = normalize_provider_scope(
            integration.provider,
            command.requested_scope,
            write_enabled=command.capability_version in REGISTERED_WRITE_CAPABILITIES,
        )
        if not scope_is_subset(requested=requested_scope, allowed=integration.scope):
            raise IntegrationValidationError("ResourceBinding scope exceeds Integration scope")
        now = datetime.now(UTC)
        statement = (
            select(ResourceBinding)
            .where(
                ResourceBinding.project_id == command.project_id,
                ResourceBinding.scope_level == command.scope_level.value,
                ResourceBinding.scope_key == command.scope_key,
                ResourceBinding.requirement_key == command.requirement_key,
            )
            .with_for_update()
        )
        row = (await self._session.scalars(statement)).one_or_none()
        revision = str(integration.revision)
        checksum = binding_checksum(
            project_id=command.project_id,
            scope_level=command.scope_level,
            scope_key=command.scope_key,
            requirement_key=command.requirement_key,
            resource_kind=command.resource_kind,
            integration_id=integration.integration_id,
            provider=integration.provider,
            capability_version=command.capability_version,
            revision=revision,
            scope=requested_scope,
        )
        if row is None:
            row = ResourceBinding(
                id=uuid4(),
                project_id=command.project_id,
                scope_level=command.scope_level.value,
                scope_key=command.scope_key,
                requirement_key=command.requirement_key,
                resource_kind=command.resource_kind,
                integration_id=integration.integration_id,
                run_id=None,
                source_binding_id=None,
                provider=integration.provider,
                capability_version=command.capability_version,
                revision=revision,
                scope_json=requested_scope,
                checksum=checksum,
                created_by=command.created_by,
                disabled_at=None,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.resource_kind = command.resource_kind
            row.integration_id = integration.integration_id
            row.provider = integration.provider
            row.capability_version = command.capability_version
            row.revision = revision
            row.scope_json = requested_scope
            row.checksum = checksum
            row.created_by = command.created_by
            row.disabled_at = None
            row.updated_at = now
        return _stored_binding(row)

    async def list_resource_bindings(
        self, *, project_id: UUID
    ) -> tuple[StoredResourceBinding, ...]:
        """Project の現在 binding と歴史 Run snapshot を列挙する。"""

        rows = await self._session.scalars(
            select(ResourceBinding)
            .where(ResourceBinding.project_id == project_id)
            .order_by(
                ResourceBinding.scope_level,
                ResourceBinding.scope_key,
                ResourceBinding.requirement_key,
            )
        )
        return tuple(_stored_binding(row) for row in rows)

    async def find_configured_binding(
        self,
        *,
        project_id: UUID,
        task_scope_key: str,
        requirement_key: str,
    ) -> StoredResourceBinding | None:
        """Task override を優先し、無ければ Project default binding を返す。"""

        rows = (
            await self._session.scalars(
                select(ResourceBinding).where(
                    ResourceBinding.project_id == project_id,
                    ResourceBinding.requirement_key == requirement_key,
                    ResourceBinding.disabled_at.is_(None),
                    ResourceBinding.scope_level.in_(
                        [
                            ResourceBindingLevel.TASK.value,
                            ResourceBindingLevel.PROJECT_DEFAULT.value,
                        ]
                    ),
                )
            )
        ).all()
        task = next(
            (
                row
                for row in rows
                if row.scope_level == ResourceBindingLevel.TASK.value
                and row.scope_key == task_scope_key
            ),
            None,
        )
        project = next(
            (
                row
                for row in rows
                if row.scope_level == ResourceBindingLevel.PROJECT_DEFAULT.value
                and row.scope_key == "project"
            ),
            None,
        )
        selected = task if task is not None else project
        return _stored_binding(selected) if selected is not None else None

    async def freeze_run_binding(
        self,
        *,
        run_id: UUID,
        project_id: UUID,
        actor_id: UUID,
        binding: ResolvedRunBinding,
    ) -> StoredResourceBinding:
        """解決済み binding を Run level の immutable snapshot として追加する。"""

        scope_key = str(run_id)
        revision = str(binding.integration.revision)
        checksum = binding_checksum(
            project_id=project_id,
            scope_level=ResourceBindingLevel.RUN,
            scope_key=scope_key,
            requirement_key=binding.requirement_key,
            resource_kind=binding.resource_kind,
            integration_id=binding.integration.integration_id,
            provider=binding.integration.provider,
            capability_version=binding.capability_version,
            revision=revision,
            scope=binding.scope,
        )
        now = datetime.now(UTC)
        row = ResourceBinding(
            id=uuid4(),
            project_id=project_id,
            scope_level=ResourceBindingLevel.RUN.value,
            scope_key=scope_key,
            requirement_key=binding.requirement_key,
            resource_kind=binding.resource_kind,
            integration_id=binding.integration.integration_id,
            run_id=run_id,
            source_binding_id=binding.source_binding_id,
            provider=binding.integration.provider,
            capability_version=binding.capability_version,
            revision=revision,
            scope_json=dict(binding.scope),
            checksum=checksum,
            created_by=actor_id,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _stored_binding(row)

    async def get_run_binding(
        self, *, run_id: UUID, requirement_key: str
    ) -> StoredResourceBinding:
        """Run に凍結された requirement binding を取得する。"""

        row = (
            await self._session.scalars(
                select(ResourceBinding).where(
                    ResourceBinding.run_id == run_id,
                    ResourceBinding.scope_level == ResourceBindingLevel.RUN.value,
                    ResourceBinding.requirement_key == requirement_key,
                )
            )
        ).one_or_none()
        if row is None:
            raise ResourceBindingNotFoundError("Run ResourceBinding was not found")
        return _stored_binding(row)

    async def get_binding(
        self, *, project_id: UUID, binding_id: UUID
    ) -> StoredResourceBinding:
        """Project ownership を確認して binding を返す。"""

        row = await self._session.get(ResourceBinding, binding_id)
        if row is None or row.project_id != project_id:
            raise ResourceBindingNotFoundError("ResourceBinding not found in project")
        return _stored_binding(row)


def _validate_binding_identity(command: PutResourceBindingCommand) -> None:
    """Binding の scope/key identity を bounded protocol key へ制限する。"""

    if not 1 <= len(command.scope_key) <= 256:
        raise IntegrationValidationError("ResourceBinding scope key is invalid")
    if (
        not 1 <= len(command.requirement_key) <= 128
        or not command.requirement_key[0].islower()
        or any(
            not (character.islower() or character.isdigit() or character in "_.-")
            for character in command.requirement_key
        )
    ):
        raise IntegrationValidationError("ResourceBinding requirement key is invalid")
    if not 1 <= len(command.resource_kind) <= 64:
        raise IntegrationValidationError("ResourceBinding kind is invalid")
    if (
        command.scope_level is ResourceBindingLevel.PROJECT_DEFAULT
        and command.scope_key != "project"
    ):
        raise IntegrationValidationError("Project default binding scope key must be project")


def _stored_secret(row: SecretReference) -> StoredSecretReference:
    """Secret locator を落として公開 read model へ変換する。"""

    return StoredSecretReference(
        secret_reference_id=row.id,
        project_id=row.project_id,
        name=row.name,
        provider=row.provider,
        resolver=SecretResolver(row.resolver),
        key_version=row.key_version,
        status=IntegrationStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        disabled_at=row.disabled_at,
    )


def _resolved_secret(
    row: SecretReference, material: EncryptedSecret | None = None
) -> ResolvedSecretReference:
    """ORM 行を Worker 内部用の解決 metadata へ変換する。MANAGED は密文も添える。"""

    return ResolvedSecretReference(
        secret_reference_id=row.id,
        project_id=row.project_id,
        provider=row.provider,
        resolver=SecretResolver(row.resolver),
        locator=row.locator,
        key_version=row.key_version,
        status=IntegrationStatus(row.status),
        managed_material=material,
    )


def _stored_integration(row: Integration) -> StoredIntegration:
    """Connection 設定値を落として公開 Integration read model へ変換する。"""

    return StoredIntegration(
        integration_id=row.id,
        project_id=row.project_id,
        name=row.name,
        kind=row.kind,
        provider=row.provider,
        status=IntegrationStatus(row.status),
        revision=row.revision,
        capabilities=tuple(row.capabilities_json),
        scope=dict(row.scope_json),
        config_keys=tuple(sorted(row.config_json)),
        secret_reference_id=row.secret_reference_id,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        disabled_at=row.disabled_at,
    )


def _resolved_integration(row: Integration) -> ResolvedIntegration:
    """ORM 行を内部解決用 Integration snapshot へ変換する。"""

    return ResolvedIntegration(
        integration_id=row.id,
        project_id=row.project_id,
        name=row.name,
        kind=row.kind,
        provider=row.provider,
        status=IntegrationStatus(row.status),
        revision=row.revision,
        capabilities=tuple(row.capabilities_json),
        scope=dict(row.scope_json),
        config=dict(row.config_json),
        secret_reference_id=row.secret_reference_id,
    )


def _stored_binding(row: ResourceBinding) -> StoredResourceBinding:
    """ORM 行を domain read model へ変換する。"""

    return StoredResourceBinding(
        binding_id=row.id,
        project_id=row.project_id,
        scope_level=ResourceBindingLevel(row.scope_level),
        scope_key=row.scope_key,
        requirement_key=row.requirement_key,
        resource_kind=row.resource_kind,
        integration_id=row.integration_id,
        run_id=row.run_id,
        source_binding_id=row.source_binding_id,
        provider=row.provider,
        capability_version=row.capability_version,
        revision=row.revision,
        scope=dict(row.scope_json),
        checksum=row.checksum,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        disabled_at=row.disabled_at,
    )
