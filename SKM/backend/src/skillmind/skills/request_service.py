"""短い transaction で原要求・一度限りの開始・撤権監査を管理する。"""

from __future__ import annotations

import hmac
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.auth.sessions import UnauthorizedSessionError, validate_session_state
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import OutboxMessage, SkillInterpretationCall, SkillInterpretationRequest
from skillmind.skills.domain import (
    SaveModelInterpretationCommand,
    SkillInterpretationStatus,
    StoredInterpretationExecution,
)
from skillmind.skills.interpretation_requests import (
    InterpretationCallPermit,
    InterpretationRequestConflictError,
    InterpretationRequestDeniedError,
    InterpretationRequestNotFoundError,
    InterpretationRequestOwner,
    InterpretationRequestSnapshot,
)
from skillmind.skills.repository import SkillRepository
from skillmind.skills.request_repository import InterpretationRequestRepository
from skillmind.users.access import authorize_user_access, validate_user_access
from skillmind.users.domain import UserAccess
from skillmind.users.repository import LockedUsers, UserRepository

INTERPRETATION_DISPATCH_TOPIC = "skill.interpret.dispatch/v1"
_ACTIVE = frozenset({"QUEUED", "RUNNING", "UNKNOWN"})


class _ResultAuthorizationExpired(RuntimeError):
    """候補の全 transaction を rollback してから撤権を別途記録する合図。"""


def _checksum(value: Mapping[str, Any]) -> str:
    """共有 canonical JSON のみで凍結入力の完全一致を検証する。"""

    return f"sha256:{sha256_hex(canonical_json(value))}"


def _owner_hash(token: str) -> str:
    """DB には原 Worker が保持する token の hash だけを保存する。"""

    return f"sha256:{sha256_hex(token.encode('utf-8'))}"


class InterpretationRequestService:
    """モデル I/O を transaction に入れず、Queue replay に開始資格を渡さない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """API/Worker と同じ PostgreSQL factory を使う。"""

        self._session_factory = session_factory

    async def accept(
        self,
        *,
        access: UserAccess,
        request_id: UUID,
        skill_source_id: UUID,
        execution_key: str,
        frozen_input: Mapping[str, Any],
    ) -> InterpretationRequestSnapshot:
        """原 credential と入力を検証し、要求と ID だけの Outbox を同時 commit する。"""

        access = deepcopy(access)
        validate_user_access(access)
        if not isinstance(request_id, UUID) or request_id.int == 0:
            raise ValueError("Interpretation request ID must be a nonzero UUID")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", execution_key) is None:
            raise ValueError("Invalid interpretation execution key")
        frozen = deepcopy(dict(frozen_input))
        checksum = _checksum(frozen)
        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access, target_id=None, include_target_sessions=False, read_only_actor=True
            )

            def authorize() -> datetime:
                """source/要求/flush の待機を含む最新時刻で原 credential を検証する。"""

                return authorize_user_access(
                    access, locked, now=datetime.now(UTC), admin=True, write=True
                )

            authorize()
            await SkillRepository(session).get_source(
                organization_id=locked.actor.organization_id, skill_source_id=skill_source_id
            )
            repository = InterpretationRequestRepository(session)
            existing = await repository.find(request_id, lock=True)
            authorize()
            if existing is not None:
                if existing.organization_id != locked.actor.organization_id:
                    raise InterpretationRequestNotFoundError("Interpretation request was not found")
                if (
                    existing.actor_id != locked.actor.id
                    or existing.auth_session_id != locked.current_session.id
                    or existing.skill_source_id != skill_source_id
                    or existing.execution_key != execution_key
                    or existing.input_checksum != checksum
                    or _checksum(existing.input_json) != checksum
                ):
                    raise InterpretationRequestConflictError(
                        "Interpretation request identity conflicts"
                    )
                return repository.snapshot(existing)
            prior = await repository.find_execution(
                organization_id=locked.actor.organization_id, execution_key=execution_key
            )
            authorize()
            if prior is not None:
                # 新 ID/新会話で UNKNOWN な元処理をやり直さない。確認は read 経路へ分ける。
                raise InterpretationRequestConflictError(
                    "Interpretation execution already has an original request",
                    existing_request_id=prior.id,
                )
            now = authorize()
            row = SkillInterpretationRequest(
                id=request_id,
                organization_id=locked.actor.organization_id,
                actor_id=locked.actor.id,
                auth_session_id=locked.current_session.id,
                accepted_http_request_id=access.request_id,
                skill_source_id=skill_source_id,
                execution_key=execution_key,
                input_json=frozen,
                input_checksum=checksum,
                status="QUEUED",
                owner_hash=None,
                claimed_at=None,
                created_at=now,
                finished_at=None,
                interpretation_id=None,
                error_code=None,
            )
            session.add(row)
            # relationship のない FK 親は Outbox と同じ transaction でも先に flush する。
            await session.flush()
            authorize()
            session.add(
                OutboxMessage(
                    id=uuid4(),
                    aggregate_type="skill_interpretation_request",
                    aggregate_id=request_id,
                    topic=INTERPRETATION_DISPATCH_TOPIC,
                    payload_json={"request_id": str(request_id)},
                    occurred_at=now,
                    published_at=None,
                    publish_attempts=0,
                    error_json=None,
                )
            )
            await session.flush()
            authorize()
            return repository.snapshot(row)

    async def confirm(
        self, *, access: UserAccess, request_id: UUID
    ) -> InterpretationRequestSnapshot:
        """現在の ADMIN に組織内の原要求だけを返し、開始や期限延長は行わない。"""

        access = deepcopy(access)
        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access, target_id=None, include_target_sessions=False, read_only_actor=True
            )
            authorize_user_access(access, locked, now=datetime.now(UTC), admin=True, write=False)
            row = await InterpretationRequestRepository(session).require(request_id)
            authorize_user_access(access, locked, now=datetime.now(UTC), admin=True, write=False)
            if row.organization_id != locked.actor.organization_id:
                raise InterpretationRequestNotFoundError("Interpretation request was not found")
            return InterpretationRequestRepository.snapshot(row)

    @asynccontextmanager
    async def _locked(
        self, request_id: UUID
    ) -> AsyncIterator[tuple[AsyncSession, SkillInterpretationRequest, Callable[[], bool]]]:
        """原参照から固定順で再読し、失効は transaction を rollback せず監査に残す。"""

        async with self._session_factory() as session, session.begin():
            repository = InterpretationRequestRepository(session)
            before = repository.snapshot(await repository.require(request_id))
            locked: LockedUsers | None = None
            # FK で通常は残るが、欠落を新しい会話で補修してはいけない。
            with suppress(UnauthorizedSessionError):
                locked = await UserRepository(session).lock_session_reference(
                    organization_id=before.organization_id,
                    user_id=before.actor_id,
                    session_id=before.auth_session_id,
                )
            row = await repository.require(request_id, lock=True)
            if (
                row.organization_id != before.organization_id
                or row.actor_id != before.actor_id
                or row.auth_session_id != before.auth_session_id
                or row.input_checksum != before.input_checksum
                or row.skill_source_id != before.skill_source_id
                or row.execution_key != before.execution_key
            ):
                raise InterpretationRequestDeniedError("Interpretation request binding changed")
            initial_status = row.status

            def authorize() -> bool:
                """全 lock 待ち後と commit 前の時刻で原会話を判定する。"""

                now = datetime.now(UTC)
                try:
                    if locked is None or locked.actor.system_role != "ADMIN":
                        raise UnauthorizedSessionError("Authentication is required")
                    validate_session_state(locked.current_session, locked.actor, now=now)
                except UnauthorizedSessionError:
                    if row.status in _ACTIVE or initial_status in _ACTIVE:
                        row.status, row.error_code, row.finished_at = (
                            "REVOKED",
                            "authorization_revoked",
                            now,
                        )
                    return False
                return True

            yield session, row, authorize

    @staticmethod
    def _owns(row: SkillInterpretationRequest, owner: InterpretationRequestOwner) -> bool:
        """同じ開始 token と完全な凍結束縛が揃う元 Worker だけを通す。"""

        snapshot = owner.request
        return (
            row.id == snapshot.request_id
            and row.organization_id == snapshot.organization_id
            and row.actor_id == snapshot.actor_id
            and row.auth_session_id == snapshot.auth_session_id
            and row.skill_source_id == snapshot.skill_source_id
            and row.execution_key == snapshot.execution_key
            and row.input_checksum == snapshot.input_checksum
            and row.input_checksum == _checksum(row.input_json) == _checksum(snapshot.input)
            and row.owner_hash is not None
            and hmac.compare_digest(row.owner_hash, _owner_hash(owner.token))
        )

    async def claim(self, request_id: UUID) -> InterpretationRequestOwner | None:
        """初回認領だけが owner を返す。commit 不明や再配送で token を取り戻さない。"""

        owner: InterpretationRequestOwner | None = None
        async with self._locked(request_id) as (session, row, authorize):
            if authorize() and row.status == "QUEUED":
                if row.input_checksum != _checksum(row.input_json):
                    row.status, row.error_code, row.finished_at = (
                        "FAILED",
                        "input_integrity",
                        datetime.now(UTC),
                    )
                else:
                    token = token_urlsafe(32)
                    row.owner_hash, row.claimed_at, row.status = (
                        _owner_hash(token),
                        datetime.now(UTC),
                        "RUNNING",
                    )
                    await session.flush()
                    if authorize():
                        owner = InterpretationRequestOwner(
                            InterpretationRequestRepository.snapshot(row), token
                        )
        return owner

    async def start_call(
        self, owner: InterpretationRequestOwner, *, ordinal: int, feedback: str | None
    ) -> InterpretationCallPermit | None:
        """初回/修復ごとの許可を一度だけ commit し、再読時には返さない。"""

        if (
            type(ordinal) is not int
            or (ordinal == 0 and feedback is not None)
            or (ordinal == 1 and (not isinstance(feedback, str) or not feedback))
            or ordinal not in (0, 1)
        ):
            raise ValueError("Invalid interpretation call ordinal or feedback")
        owner = deepcopy(owner)
        permit: InterpretationCallPermit | None = None
        async with self._locked(owner.request.request_id) as (session, row, authorize):
            if authorize() and row.status == "RUNNING" and self._owns(row, owner):
                calls = await InterpretationRequestRepository(session).calls(row.id)
                # 修復は同じ owner の初回 return を観測した場合だけ許可する。
                if (
                    authorize()
                    and len(calls) == ordinal
                    and (ordinal == 0 or calls[0].returned_at is not None)
                ):
                    call = SkillInterpretationCall(
                        id=uuid4(),
                        request_id=row.id,
                        ordinal=ordinal,
                        feedback=feedback,
                        granted_at=datetime.now(UTC),
                        returned_at=None,
                    )
                    session.add(call)
                    await session.flush()
                    if authorize():
                        permit = InterpretationCallPermit(owner, call.id, ordinal)
        return permit

    async def record_return(self, permit: InterpretationCallPermit) -> None:
        """return 観測は撤権後も記録するが、新しい開始権や停止証明にはしない。"""

        permit = deepcopy(permit)
        async with self._locked(permit.owner.request.request_id) as (session, row, authorize):
            if not self._owns(row, permit.owner):
                raise InterpretationRequestDeniedError("Interpretation call owner was rejected")
            calls = await InterpretationRequestRepository(session).calls(row.id)
            call = next(
                (
                    item
                    for item in calls
                    if item.id == permit.call_id and item.ordinal == permit.ordinal
                ),
                None,
            )
            if call is None:
                raise InterpretationRequestDeniedError("Interpretation call was not granted")
            if call.returned_at is None:
                call.returned_at = max(datetime.now(UTC), call.granted_at)
            authorize()
            await session.flush()
            authorize()

    async def finish(
        self,
        owner: InterpretationRequestOwner,
        command: SaveModelInterpretationCommand | None,
        *,
        existing_id: UUID | None = None,
    ) -> StoredInterpretationExecution:
        """結果と要求終態を原会話の同一 transaction に保存し、撤権後の成果採用を止める。"""

        owner, command = deepcopy(owner), deepcopy(command)
        if (command is None) == (existing_id is None):
            raise ValueError("Exactly one interpretation result source is required")
        stored: StoredInterpretationExecution | None = None
        try:
            async with self._locked(owner.request.request_id) as (session, row, authorize):
                if authorize() and row.status in {"RUNNING", "UNKNOWN"} and self._owns(row, owner):
                    if command is not None and (
                        command.organization_id != row.organization_id
                        or command.skill_source_id != row.skill_source_id
                        or command.execution_key != row.execution_key
                        or command.model != row.input_json.get("model")
                    ):
                        raise InterpretationRequestDeniedError(
                            "Interpretation result binding was rejected"
                        )
                    calls = await InterpretationRequestRepository(session).calls(row.id)
                    if not authorize():
                        raise _ResultAuthorizationExpired
                    if any(call.returned_at is None for call in calls):
                        raise InterpretationRequestDeniedError(
                            "Interpretation call return is unknown"
                        )
                    if (
                        command is not None
                        and not calls
                        and command.execution.get("error_code") != "unsafe_source"
                    ):
                        # 既存成果の復用は許すが、呼出しなしに新しい model 成果を補造しない。
                        existing = await SkillRepository(session).find_model_interpretation(
                            organization_id=row.organization_id,
                            skill_source_id=row.skill_source_id,
                            interpreter_version=command.interpreter_version,
                            execution_key=row.execution_key,
                        )
                        if existing is None:
                            raise InterpretationRequestDeniedError(
                                "Interpretation result has no observed call"
                            )
                    if not authorize():
                        raise _ResultAuthorizationExpired
                    if command is not None:
                        stored = await SkillRepository(session).save_model_interpretation(command)
                    else:
                        assert existing_id is not None
                        stored = await SkillRepository(session).get_model_interpretation(
                            organization_id=row.organization_id, interpretation_id=existing_id
                        )
                        if (
                            stored.skill_source_id != row.skill_source_id
                            or stored.execution_key != row.execution_key
                            or stored.model != row.input_json.get("model")
                        ):
                            raise InterpretationRequestDeniedError(
                                "Existing interpretation binding was rejected"
                            )
                    row.interpretation_id = stored.interpretation_id
                    row.status = (
                        "SUCCEEDED"
                        if stored.status is SkillInterpretationStatus.PREVIEW_READY
                        else "FAILED"
                    )
                    row.error_code = (
                        None
                        if row.status == "SUCCEEDED"
                        else (stored.error_code or "interpretation_failed")
                    )
                    row.finished_at = datetime.now(UTC)
                    await session.flush()
                    if not authorize():
                        # 候補と終態を含めて rollback し、部分採用を残さない。
                        raise _ResultAuthorizationExpired
        except _ResultAuthorizationExpired:
            async with self._locked(owner.request.request_id) as (_session, _row, authorize):
                authorize()
            raise InterpretationRequestDeniedError(
                "Interpretation request cannot accept a result"
            ) from None
        if stored is None:
            raise InterpretationRequestDeniedError("Interpretation request cannot accept a result")
        return stored

    async def mark_unknown(self, owner: InterpretationRequestOwner) -> None:
        """元 Worker の中断を状態不明として残し、失敗やモデル停止を補造しない。"""

        owner = deepcopy(owner)
        async with self._locked(owner.request.request_id) as (_session, row, authorize):
            if authorize() and row.status == "RUNNING" and self._owns(row, owner):
                row.status = "UNKNOWN"

    async def fail_before_call(self, owner: InterpretationRequestOwner, *, error_code: str) -> bool:
        """開始許可が一つも無い準備失敗だけを、モデル実行の失敗と区別して確定する。"""

        if error_code not in {"input_integrity", "interpreter_unavailable", "source_unavailable"}:
            raise ValueError("Unsupported interpretation preparation failure")
        owner = deepcopy(owner)
        failed = False
        async with self._locked(owner.request.request_id) as (session, row, authorize):
            if authorize() and row.status == "RUNNING" and self._owns(row, owner):
                calls = await InterpretationRequestRepository(session).calls(row.id)
                if authorize() and not calls:
                    row.status, row.error_code, row.finished_at = (
                        "FAILED",
                        error_code,
                        datetime.now(UTC),
                    )
                    await session.flush()
                    failed = authorize()
        return failed

    async def recover_unknown(self, *, before: datetime, limit: int) -> int:
        """古い RUNNING は UNKNOWN/撤権へ移し、停止・再開始・新 Outbox を補造しない。"""

        if limit < 1 or before.tzinfo is None:
            raise ValueError("Recovery requires a positive limit and timezone-aware cutoff")
        async with self._session_factory() as session:
            candidates = await InterpretationRequestRepository(session).stale_running(
                before=before, limit=limit
            )
        changed = 0
        for request_id in candidates:
            async with self._locked(request_id) as (_session, row, authorize):
                if (
                    row.status == "RUNNING"
                    and row.claimed_at is not None
                    and row.claimed_at <= before
                ):
                    if authorize():
                        row.status = "UNKNOWN"
                    changed += 1
        return changed
