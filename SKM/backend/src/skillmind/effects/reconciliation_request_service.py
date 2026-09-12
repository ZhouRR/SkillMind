"""現在の会話と原要求を短い transaction で核対台帳の受付・実行・保存へ接続する。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.auth.sessions import UnauthorizedSessionError, validate_session_state
from skillmind.db.models import EffectReconciliationRequest
from skillmind.documents.library import DocumentLibraryTarget
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationDeniedError,
    EffectReconciliationObservation,
    EffectReconciliationReference,
    EffectReconciliationTarget,
)
from skillmind.effects.reconciliation_repository import ReconciliationRequestRepository
from skillmind.effects.reconciliation_requests import (
    RECONCILIATION_QUEUE_TIMEOUT_SECONDS,
    ReconciliationRequestConflictError,
    ReconciliationRequestNotFoundError,
    ReconciliationRequestOwner,
    ReconciliationRequestSnapshot,
    reconciliation_target_checksum,
)
from skillmind.projects.domain import ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.repository import RunRepository
from skillmind.users.access import authorize_user_access, validate_user_access
from skillmind.users.domain import UserAccess
from skillmind.users.repository import LockedUsers, UserRepository


class _ResultAuthorizationLost(RuntimeError):
    """flush 後に失権した観測の transaction 全体を rollback する内部合図。"""


class ReconciliationRequestService:
    """秘密の解決や外部 I/O を持たず、API と Worker の同じ永続要求を管理する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        document_library_target: DocumentLibraryTarget | None,
    ) -> None:
        """既存 DB と文書庫の帰属だけを受け取り、外部 writer を組み立てない。"""
        self._session_factory = session_factory
        self._document_library_target = document_library_target

    async def _target(
        self,
        session: AsyncSession,
        reference: EffectReconciliationReference,
    ) -> EffectReconciliationTarget:
        """原提案の共通検証で対象を復元し、API では Secret を解決しない。"""
        return await RunRepository(
            session,
            document_library_target=self._document_library_target,
        ).load_effect_reconciliation_target(
            project_id=reference.project_id,
            run_id=reference.run_id,
            effect_execution_id=reference.effect_execution_id,
        )

    async def accept(
        self,
        *,
        access: UserAccess,
        request_id: UUID,
        project_id: UUID,
        run_id: UUID,
        effect_execution_id: UUID,
    ) -> ReconciliationRequestSnapshot:
        """原 credential/CSRF と現在の読取所属の下で要求と dispatch を同時受理する。"""
        access = deepcopy(access)
        validate_user_access(access)
        if any(
            not isinstance(i, UUID) or i.int == 0
            for i in (
                request_id,
                project_id,
                run_id,
                effect_execution_id,
            )
        ):
            raise ValueError("Reconciliation identity is invalid")
        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
                read_only_actor=True,
            )

            def authorize() -> datetime:
                """DB/flush の待機後も同じ credential と最新時刻で受理権を確認する。"""
                return authorize_user_access(
                    access,
                    locked,
                    now=datetime.now(UTC),
                    admin=False,
                    write=True,
                )

            authorize()
            project = await ProjectRepository(session).lock_write_access(
                user=locked.actor,
                project_id=project_id,
            )
            ProjectRepository.require_read_access(project)
            reference = EffectReconciliationReference(
                locked.actor.organization_id,
                locked.actor.id,
                locked.current_session.id,
                project_id,
                run_id,
                effect_execution_id,
            )
            repository = ReconciliationRequestRepository(session)
            # Org gate が全受理を直列化する。新規時は Run→request の順でのみ lock を取る。
            existing = await repository.find(request_id)
            if existing is not None:
                authorize()
                if existing.organization_id != reference.organization_id:
                    raise ReconciliationRequestNotFoundError("Reconciliation request was not found")
                if repository.reference(existing) != reference:
                    raise ReconciliationRequestConflictError(
                        "Reconciliation request identity conflicts"
                    )
                return repository.snapshot(existing)
            try:
                target = await self._target(session, reference)
            except LookupError:
                raise ReconciliationRequestNotFoundError("Original effect was not found") from None
            row = await repository.create(
                request_id=request_id,
                reference=reference,
                accepted_http_request_id=access.request_id,
                target=target,
                now=authorize(),
            )
            await session.flush()
            authorize()
            snapshot = repository.snapshot(row)
        return snapshot

    async def confirm(
        self,
        *,
        access: UserAccess,
        request_id: UUID,
        project_id: UUID,
        run_id: UUID,
    ) -> ReconciliationRequestSnapshot:
        """現在の Project 読取権で原要求を確認し、owner/会話の再発行や再 dispatch はしない。"""
        async with self._read_access(access, project_id) as (session, organization_id, authorize):
            repository = ReconciliationRequestRepository(session)
            row = await repository.require(request_id)
            if (
                row.organization_id != organization_id
                or row.project_id != project_id
                or row.run_id != run_id
            ):
                raise ReconciliationRequestNotFoundError("Reconciliation request was not found")
            authorize()
            return repository.snapshot(row)

    async def latest(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        run_id: UUID,
        effect_execution_id: UUID,
    ) -> ReconciliationRequestSnapshot | None:
        """現在の所属で最新観測を読み、過去の台帳や秘密を一括公開しない。"""
        async with self._read_access(access, project_id) as (session, organization_id, authorize):
            repository = ReconciliationRequestRepository(session)
            row = await repository.latest(
                project_id=project_id,
                run_id=run_id,
                effect_execution_id=effect_execution_id,
            )
            if row is not None and row.organization_id != organization_id:
                raise ReconciliationRequestNotFoundError("Reconciliation request was not found")
            authorize()
            return None if row is None else repository.snapshot(row)

    @asynccontextmanager
    async def _read_access(
        self,
        access: UserAccess,
        project_id: UUID,
    ) -> AsyncIterator[tuple[AsyncSession, UUID, Callable[[], None]]]:
        """原要求確認と最新観測で現在会話・Project 読取権の同じ検証を使う。"""
        access = deepcopy(access)
        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
                read_only_actor=True,
            )

            def authorize() -> None:
                """すべての待機後にも元 credential と新しい時刻を使う。"""
                authorize_user_access(
                    access, locked, now=datetime.now(UTC), admin=False, write=False
                )

            authorize()
            project = await ProjectRepository(session).lock_write_access(
                user=locked.actor,
                project_id=project_id,
            )
            ProjectRepository.require_read_access(project)
            yield session, locked.actor.organization_id, authorize

    @asynccontextmanager
    async def _locked(
        self,
        request_id: UUID,
        *,
        load_target: bool = True,
    ) -> AsyncIterator[
        tuple[
            AsyncSession,
            EffectReconciliationRequest,
            EffectReconciliationTarget | None,
            Callable[[], bool],
        ]
    ]:
        """永続参照から Org→User→Session→Project→Run→request の順で再読する。"""
        async with self._session_factory() as session, session.begin():
            repository = ReconciliationRequestRepository(session)
            before = repository.snapshot(await repository.require(request_id))
            reference = before.reference
            locked: LockedUsers | None = None
            failure: str | None = None
            target = None
            try:
                locked = await UserRepository(session).lock_session_reference(
                    organization_id=reference.organization_id,
                    user_id=reference.actor_id,
                    session_id=reference.auth_session_id,
                )
                validate_session_state(locked.current_session, locked.actor, now=datetime.now(UTC))
                project = await ProjectRepository(session).lock_write_access(
                    user=locked.actor,
                    project_id=reference.project_id,
                )
                ProjectRepository.require_read_access(project)
            except (UnauthorizedSessionError, ProjectNotFoundError):
                failure = "authorization_revoked"
            if failure is None and load_target:
                try:
                    target = await self._target(session, reference)
                    if reconciliation_target_checksum(target) != before.target_checksum:
                        failure = "target_changed"
                except (ValueError, LookupError):
                    failure = "target_changed"
            row = await repository.require(request_id, lock=True)
            if (
                repository.reference(row) != reference
                or row.target_checksum != before.target_checksum
                or row.command_checksum != before.command_checksum
                or row.kind != before.kind
                or row.created_at != before.created_at
            ):
                raise EffectReconciliationDeniedError("Original reconciliation request changed")

            def authorize() -> bool:
                """待機後の会話/期限を確認し、active 要求だけを失効として閉じる。"""
                reason = failure
                now = datetime.now(UTC)
                try:
                    if locked is None:
                        raise UnauthorizedSessionError("Authentication is required")
                    validate_session_state(locked.current_session, locked.actor, now=now)
                except UnauthorizedSessionError:
                    reason = "authorization_revoked"
                if (
                    reason is None
                    and row.status == "RUNNING"
                    and (row.lease_expires_at is None or row.lease_expires_at <= now)
                ):
                    reason = "lookup_interrupted"
                if (
                    reason is None
                    and row.status == "QUEUED"
                    and row.created_at
                    <= now
                    - timedelta(
                        seconds=RECONCILIATION_QUEUE_TIMEOUT_SECONDS,
                    )
                ):
                    reason = "lookup_interrupted"
                if reason is not None:
                    repository.fail(row, code=reason, now=now)
                    return False
                return True

            yield session, row, target, authorize

    async def claim(self, request_id: UUID) -> ReconciliationRequestOwner | None:
        """初回 commit の返却を確認した Worker にだけ token を返す。再配送は read を始めない。"""
        owner = None
        async with self._locked(request_id) as (session, row, _target, authorize):
            if authorize():
                owner = ReconciliationRequestRepository.claim(
                    row,
                    token=token_urlsafe(32),
                    now=datetime.now(UTC),
                )
                await session.flush()
                if not authorize():
                    owner = None
        return owner

    async def authorize(self, owner: ReconciliationRequestOwner) -> EffectReconciliationTarget:
        """照会の前後に原 owner/受理対象/現在会話を復験し、失効記録は commit 後に通知する。"""
        owner = deepcopy(owner)
        target = None
        async with self._locked(owner.request.request_id) as (_session, row, current, authorize):
            if authorize():
                ReconciliationRequestRepository.require_owner(row, owner, now=datetime.now(UTC))
                target = current
        if target is None:
            raise EffectReconciliationDeniedError("Reconciliation lookup no longer authorized")
        return target

    async def finish(
        self,
        owner: ReconciliationRequestOwner,
        observation: EffectReconciliationObservation,
    ) -> ReconciliationRequestSnapshot:
        """同じ受理対象と owner の観測だけを保存し、flush 後の失権では全観測を rollback する。"""
        owner, observation = deepcopy(owner), deepcopy(observation)
        snapshot = None
        try:
            async with self._locked(owner.request.request_id) as (session, row, target, authorize):
                if authorize() and target is not None:
                    replay = row.status == "SUCCEEDED"
                    ReconciliationRequestRepository.finish(
                        row,
                        owner,
                        observation=observation,
                        target=target,
                        now=datetime.now(UTC),
                    )
                    await session.flush()
                    if not authorize() or (
                        not replay
                        and (
                            row.lease_expires_at is None
                            or row.lease_expires_at <= datetime.now(UTC)
                        )
                    ):
                        raise _ResultAuthorizationLost
                    snapshot = ReconciliationRequestRepository.snapshot(row)
        except _ResultAuthorizationLost:
            await self.fail(owner, code="lookup_interrupted")
        if snapshot is None:
            raise EffectReconciliationDeniedError("Reconciliation result was not accepted")
        return snapshot

    async def fail(self, owner: ReconciliationRequestOwner, *, code: str) -> None:
        """元 owner の読取中断だけを閉じ、外部効果や確定済み観測を変更しない。"""
        owner = deepcopy(owner)
        async with self._locked(owner.request.request_id, load_target=False) as (
            _session,
            row,
            _target,
            authorize,
        ):
            ReconciliationRequestRepository.require_owner(
                row,
                owner,
                now=datetime.now(UTC),
                completed=True,
            )
            if authorize():
                ReconciliationRequestRepository.fail(row, code=code, now=datetime.now(UTC))

    async def recover_expired(self, *, limit: int) -> int:
        """期限切れの只読要求を閉じ、新 token、再照会、外部 write を発行しない。"""
        if limit < 1 or limit > 1000:
            raise ValueError("Reconciliation recovery limit must be between 1 and 1000")
        async with self._session_factory() as session:
            candidates = await ReconciliationRequestRepository(session).expired(
                now=datetime.now(UTC),
                limit=limit,
            )
        changed = 0
        for request_id in candidates:
            async with self._locked(request_id, load_target=False) as (
                _session,
                row,
                _target,
                authorize,
            ):
                previous = row.status
                authorize()
                changed += previous in {"QUEUED", "RUNNING"} and row.status != previous
        return changed
