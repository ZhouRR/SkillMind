"""Worker 内で原 Effect を只読照会し、現在の参照権と元保存先を前後に検証する。"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.run_binding import load_bound_run_resource, resolve_binding_secret
from skillmind.auth.sessions import UnauthorizedSessionError, validate_session_state
from skillmind.documents.library import DocumentLibraryTarget
from skillmind.effects.database_write import DatabaseWriteCommand
from skillmind.effects.postgres_write import DatabaseWriteConflictError, DatabaseWriteReceipt
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationDeniedError,
    EffectReconciliationInput,
    EffectReconciliationObservation,
    EffectReconciliationReference,
    EffectReconciliationUnavailableError,
)
from skillmind.effects.reconciliation_requests import reconciliation_target_checksum
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.projects.domain import ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.repository import RunRepository
from skillmind.storage.blob import StorageNamespace
from skillmind.storage.effect_write import (
    ObjectWriteCommand,
    ObjectWriteConflictError,
    ObjectWriteReceipt,
)
from skillmind.users.repository import UserRepository


class DatabaseReceiptReader(Protocol):
    """照会 use case に apply を渡さない只読 port。"""

    async def lookup(
        self,
        config: Mapping[str, Any],
        password: str,
        command: DatabaseWriteCommand,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> DatabaseWriteReceipt | None:
        """原 transaction 回执だけを READ ONLY で調べる。"""
        ...


class ObjectReceiptReader(Protocol):
    """照会 use case に PUT/公開/削除を渡さない只読 port。"""

    @property
    def namespace(self) -> StorageNamespace:
        """現在の保存先を元 command と照合する。"""
        ...

    async def lookup(
        self,
        command: ObjectWriteCommand,
        *,
        authorize: Callable[[], Awaitable[None]],
    ) -> ObjectWriteReceipt | None:
        """元 object の metadata、MIME、実 byte を GET で照合する。"""
        ...


class EffectReconciliationService:
    """原会話参照を持つ内部 Worker 用。受理台帳/Queue/API への接続前に公開してはならない。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        secret_resolver: DeploymentSecretResolver,
        database_reader: DatabaseReceiptReader,
        document_reader: ObjectReceiptReader | None,
        document_library_target: DocumentLibraryTarget | None,
    ) -> None:
        """既存接続の只読 port と現在 namespace を受け取り、書込 Provider を登録しない。"""

        self._session_factory = session_factory
        self._secret_resolver = secret_resolver
        self._database_reader = database_reader
        self._document_reader = document_reader
        self._document_library_target = document_library_target

    async def observe(
        self,
        reference: EffectReconciliationReference,
        *,
        accepted_target_checksum: str | None = None,
        authorize_request: Callable[[], Awaitable[None]] | None = None,
    ) -> EffectReconciliationObservation:
        """一回の観測だけを返し、不在/競合/timeout を再送許可や APPLIED に変換しない。"""

        try:
            async with asyncio.timeout(30):
                if authorize_request is not None:
                    await authorize_request()
                original = await self._load(reference)
                if (
                    accepted_target_checksum is not None
                    and reconciliation_target_checksum(original.target) != accepted_target_checksum
                ):
                    raise EffectReconciliationDeniedError("Accepted read target changed")

                async def authorize() -> None:
                    """現在参照権と元要求を再構築し、秘密や接続先が変われば I/O/返却を拒否する。"""
                    if authorize_request is not None:
                        await authorize_request()
                    current = await self._load(reference)
                    if current.target != original.target or not hmac.compare_digest(
                        (current.credential or "").encode(),
                        (original.credential or "").encode(),
                    ):
                        raise EffectReconciliationDeniedError("Original read target changed")

                command = original.target.command
                receipt: DatabaseWriteReceipt | ObjectWriteReceipt | None = None
                observed: Literal["CONFIRMED", "NOT_OBSERVED", "CONFLICT"] = "NOT_OBSERVED"
                kind: Literal["DATABASE_TRANSACTION", "DOCUMENT_OBJECT"] = (
                    "DATABASE_TRANSACTION"
                    if isinstance(command, DatabaseWriteCommand)
                    else "DOCUMENT_OBJECT"
                )
                try:
                    if isinstance(command, DatabaseWriteCommand):
                        if original.credential is None:
                            raise EffectReconciliationUnavailableError(
                                "Original credential unavailable"
                            )
                        receipt = await self._database_reader.lookup(
                            json.loads(original.target.config_json),
                            original.credential,
                            command,
                            authorize=authorize,
                        )
                    else:
                        if (
                            self._document_reader is None
                            or self._document_reader.namespace != command.namespace
                        ):
                            raise EffectReconciliationUnavailableError(
                                "Original storage unavailable"
                            )
                        receipt = await self._document_reader.lookup(command, authorize=authorize)
                    if receipt is not None:
                        observed = "CONFIRMED"
                except (DatabaseWriteConflictError, ObjectWriteConflictError):
                    observed = "CONFLICT"
                await authorize()
                return EffectReconciliationObservation(
                    reference.effect_execution_id,
                    kind,
                    observed,
                    datetime.now(UTC),
                    command.checksum
                    if isinstance(command, DatabaseWriteCommand)
                    else command.request_checksum,
                    receipt,
                )
        except (EffectReconciliationDeniedError, UnauthorizedSessionError, ProjectNotFoundError):
            raise EffectReconciliationDeniedError("Original read authority unavailable") from None
        except Exception:
            # 例外本文には接続先/SQL/原行が含まれ得る。取消は伝播し、観測を捏造しない。
            raise EffectReconciliationUnavailableError(
                "Original effect lookup unavailable"
            ) from None

    async def _load(
        self,
        reference: EffectReconciliationReference,
    ) -> EffectReconciliationInput:
        """短い DB TX に現在の会話/所属と元 snapshot の検査を閉じ込め、I/O 前に解放する。"""

        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_session_reference(
                organization_id=reference.organization_id,
                user_id=reference.actor_id,
                session_id=reference.auth_session_id,
            )
            validate_session_state(locked.current_session, locked.actor, now=datetime.now(UTC))
            access = await ProjectRepository(session).lock_write_access(
                user=locked.actor,
                project_id=reference.project_id,
            )
            ProjectRepository.require_read_access(access)
            target = await RunRepository(
                session,
                document_library_target=self._document_library_target,
            ).load_effect_reconciliation_target(
                project_id=reference.project_id,
                run_id=reference.run_id,
                effect_execution_id=reference.effect_execution_id,
            )
            credential = None
            if isinstance(target.command, DatabaseWriteCommand):
                bound = await load_bound_run_resource(
                    session,
                    project_id=reference.project_id,
                    run_id=reference.run_id,
                    binding_id=target.binding_id,
                    integration_id=target.command.integration_id,
                    provider=target.provider,
                    capability="database.write/v1",
                )
                if (
                    bound.integration.secret_reference_id != target.secret_reference_id
                    or bound.integration.config != json.loads(target.config_json)
                ):
                    raise EffectReconciliationDeniedError("Original database binding changed")
                credential = await resolve_binding_secret(
                    session,
                    resolver=self._secret_resolver,
                    integration=bound.integration,
                    required=True,
                )
            validate_session_state(locked.current_session, locked.actor, now=datetime.now(UTC))
            return EffectReconciliationInput(target, credential)
