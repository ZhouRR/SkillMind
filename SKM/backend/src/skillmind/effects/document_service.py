"""原 Effect の認可と文書庫 ledger を、同一の短い database transaction にまとめる。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.documents.domain import StoredDocument
from skillmind.documents.effect_repository import DocumentEffectRepository
from skillmind.documents.library import (
    DOCUMENT_LIBRARY_PROVIDER,
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryTarget,
    document_library_scope,
)
from skillmind.effects.document_command import load_document_effect_command
from skillmind.effects.document_write import (
    DOCUMENT_WRITE_PROVIDER_VERSION,
    document_proposal_payload,
)
from skillmind.effects.domain import ClaimedEffectExecution
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.repository import RunRepository
from skillmind.storage.effect_write import (
    ObjectWriteCommand,
    ObjectWriteReceipt,
)
from skillmind.storage.validation import UploadLimits


@dataclass(frozen=True, slots=True)
class PreparedDocumentWrite:
    """commit 済み予約と既知の原回执。これだけでは PUT 権を与えない。"""

    command: ObjectWriteCommand
    receipt: ObjectWriteReceipt | None


class DocumentEffectService:
    """外部 I/O を持たず、Org/原批准/lease と成果占用を同じ transaction で検証する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        features: ExecutionFeatures,
        target: DocumentLibraryTarget,
        limits: UploadLimits,
    ) -> None:
        """既存文書庫と共有 upload 配額だけを composition から受け取る。"""

        self._session_factory = session_factory
        self._features = features
        self._target = target
        self._limits = limits

    async def prepare(self, execution: ClaimedEffectExecution) -> PreparedDocumentWrite:
        """原 Artifact の byte を読み、予約が commit した後でのみ固定要求を返す。"""

        async with self._stage(execution) as (repository, command):
            prepared = PreparedDocumentWrite(command, await repository.verified_receipt(command))
        return prepared

    async def start_once(
        self, execution: ClaimedEffectExecution, command: ObjectWriteCommand
    ) -> bool:
        """SENT の commit 応答が確認できた一回だけ True。commit 未知を送信権にしない。"""

        async with self._stage(execution, expected=command) as (repository, current):
            started = await repository.start_once(current, owner_id=uuid4(), now=datetime.now(UTC))
        return started

    async def publish(
        self,
        execution: ClaimedEffectExecution,
        command: ObjectWriteCommand,
        receipt: ObjectWriteReceipt,
    ) -> StoredDocument:
        """原回执と目録を一緒に commit し、結果応答が失われても元文書 ID を再現する。"""

        async with self._stage(execution, expected=command) as (repository, current):
            await repository.record_verified(current, receipt, now=datetime.now(UTC))
            document = await repository.publish(current, now=datetime.now(UTC))
        return document

    async def authorize(self, execution: ClaimedEffectExecution) -> None:
        """ネットワークの直前/直後に原実行権を再検査し、完了時には全 DB lock を解放する。"""

        frozen = deepcopy(execution)
        self._validate_target(frozen)
        async with self._session_factory() as session, session.begin():
            await self._run_repository(session).authorize_effect_step(
                frozen, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
            )

    @asynccontextmanager
    async def _stage(
        self,
        execution: ClaimedEffectExecution,
        *,
        expected: ObjectWriteCommand | None = None,
    ) -> AsyncIterator[tuple[DocumentEffectRepository, ObjectWriteCommand]]:
        """共有認可で Org lock を取り、待機を含む ledger 操作後にも期限を再検査する。"""

        frozen = deepcopy(execution)
        self._validate_target(frozen)
        async with self._session_factory() as session, session.begin():
            runs = self._run_repository(session)
            authority = await runs.authorize_effect_step(
                frozen, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
            )
            payload = document_proposal_payload(
                operation=frozen.operation,
                target=frozen.target,
                changes=frozen.changes,
                precondition=frozen.precondition,
                verification=frozen.verification,
                scope=frozen.integration_scope,
            )
            command = await load_document_effect_command(
                session,
                effect_id=frozen.effect_execution_id,
                project_id=frozen.project_id,
                run_id=frozen.run_id,
                payload=payload,
                target=self._target,
            )
            if expected is not None and command != expected:
                raise ValueError("Document effect command changed after preparation")
            repository = DocumentEffectRepository(session)
            folder, _, name = payload["path"].rpartition("/")
            # 再予約も原 actor/Org を照合する。既存 row の占用は重複計上しない。
            await repository.reserve(
                command,
                organization_id=authority.organization_id,
                actor_id=authority.actor_id,
                folder=folder,
                name=name,
                limits=self._limits,
                now=datetime.now(UTC),
            )
            yield repository, command
            await session.flush()
            if (
                await runs.authorize_effect_step(
                    frozen, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
                )
                != authority
            ):
                raise PermissionError("Document effect authority changed during its stage")

    def _run_repository(self, session: AsyncSession) -> RunRepository:
        """提案/批准/claim と同じ capability 上限と保存先を共有する。"""

        return RunRepository(
            session, execution_features=self._features, document_library_target=self._target
        )

    def _validate_target(self, execution: ClaimedEffectExecution) -> None:
        """内部資源の原 scope と現在構成を照合し、Integration や別 switch の流用を拒否する。"""

        self._features.require_effect(execution.capability_version, execution.operation)
        project_id, target = document_library_scope(execution.integration_scope)
        if (
            execution.capability_version != DOCUMENT_WRITE_CAPABILITY
            or execution.provider != DOCUMENT_LIBRARY_PROVIDER
            or execution.integration_id is not None
            or execution.secret_reference_id is not None
            or execution.integration_config != {}
            or project_id != execution.project_id
            or target != self._target
        ):
            raise ValueError("Document effect target is unavailable")
