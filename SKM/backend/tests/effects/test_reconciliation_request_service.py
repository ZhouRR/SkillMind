"""実 SQLite 台帳/TX と共有会話検証を接続する。認可 SQL/PG lock は別回帰の対象。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from skillmind.auth.domain import SESSION_CREDENTIAL_VERSION, generate_session_credentials
from skillmind.auth.service import AuthenticatedActor
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import EffectReconciliationRequest, OutboxMessage
from skillmind.effects.reconciliation_domain import EffectReconciliationDeniedError
from skillmind.effects.reconciliation_execution import EffectReconciliationExecutor
from skillmind.effects.reconciliation_request_service import ReconciliationRequestService
from skillmind.effects.reconciliation_requests import ReconciliationRequestConflictError
from skillmind.projects.domain import ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.repository import RunRepository
from skillmind.users.domain import UserAccess
from skillmind.users.repository import LockedUsers, UserRepository
from tests.effects.test_reconciliation_repository import SqlSession, observation
from tests.effects.test_reconciliation_repository import database as database
from tests.effects.test_reconciliation_repository import original as original


@pytest.fixture
def harness(database, original, monkeypatch):
    """毎回別 Session の実 commit/rollback を使い、会話状態と時刻を操作可能にする。"""
    credentials = generate_session_credentials()
    reference = original["reference"]
    now = original["now"]
    user = SimpleNamespace(
        id=reference.actor_id,
        organization_id=reference.organization_id,
        status="ACTIVE",
        system_role="USER",
    )
    current = SimpleNamespace(
        id=reference.auth_session_id,
        user_id=user.id,
        revoked_at=None,
        idle_expires_at=now + timedelta(minutes=5),
        absolute_expires_at=now + timedelta(minutes=10),
        credential_version=SESSION_CREDENTIAL_VERSION,
        system_role_at_login="USER",
        token_hash=credentials.session_token_hash,
        csrf_token_hash=credentials.csrf_token_hash,
    )
    member = SimpleNamespace(project_id=reference.project_id, user_id=user.id, status="ACTIVE")
    project = SimpleNamespace(
        id=reference.project_id, organization_id=user.organization_id, status="ARCHIVED"
    )
    locked = LockedUsers(user, None, current, (current,))
    locks = AsyncMock(return_value=locked)
    monkeypatch.setattr(UserRepository, "lock_users", locks)
    references = AsyncMock(return_value=locked)
    monkeypatch.setattr(UserRepository, "lock_session_reference", references)
    monkeypatch.setattr(
        ProjectRepository,
        "lock_write_access",
        AsyncMock(return_value=SimpleNamespace(user=user, project=project, member=member)),
    )
    load = AsyncMock(return_value=original["target"])
    monkeypatch.setattr(RunRepository, "load_effect_reconciliation_target", load)
    access = UserAccess(
        AuthenticatedActor(
            user.id, user.organization_id, "reader@example.invalid", "Reader", "USER"
        ),
        uuid4(),
        credentials.session_token,
        credentials.csrf_token,
    )
    h = SimpleNamespace(**locals(), open_transactions=0, flush_hook=None, lose_commit=False)

    class ServiceSession(SqlSession):
        """実 transaction の adapter に終了後応答喪失と flush 待ちを注入する。"""

        async def __aenter__(self):
            """service が factory を開く度に新しい DB transaction を作る。"""
            self.context = database()
            _, self.session = self.context.__enter__()
            h.open_transactions += 1
            return self

        async def __aexit__(self, kind, value, traceback):
            """commit 完了後の応答喪失を、rollback と区別して再現する。"""
            try:
                result = self.context.__exit__(kind, value, traceback)
                if kind is None and h.lose_commit:
                    h.lose_commit = False
                    raise OSError("commit response lost")
                return result
            finally:
                h.open_transactions -= 1

        @asynccontextmanager
        async def begin(self):
            """外側 context が commit/rollback を実行する。"""
            yield

        async def flush(self):
            """制約確認後に一回だけ待機 hook を呼ぶ。"""
            await super().flush()
            if h.flush_hook is not None:
                hook, h.flush_hook = h.flush_hook, None
                hook()

        async def scalars(self, query):
            """期限回収の実 SQL を同じ DB へ渡す。"""
            return self.session.scalars(query)

    h.service = ReconciliationRequestService(
        lambda: ServiceSession(None), document_library_target=None
    )
    h.accept_args = dict(
        access=access,
        request_id=original["request_id"],
        project_id=reference.project_id,
        run_id=reference.run_id,
        effect_execution_id=reference.effect_execution_id,
    )
    return h


async def accepted(h):
    """認証済み受理を原 ID で実行する。"""
    return await h.service.accept(**h.accept_args)


async def stored(h):
    """別 Session から commit 済み原行を取得する。"""
    with h.database() as (repo, _):
        return await repo.require(h.original["request_id"])


async def test_accept_replay_requires_original_session_and_keeps_single_outbox(harness):
    """帰档 Project の読取者を許し、応答喪失後も同じ要求の受理事実だけを返す。"""
    h = harness
    h.lose_commit = True
    with pytest.raises(OSError):
        await accepted(h)
    first = await accepted(h)
    assert first.status == "QUEUED" and h.load.await_count == 1
    with h.database() as (_, session):
        messages = list(session.scalars(sa.select(OutboxMessage)))
        assert len(messages) == 1
        assert messages[0].payload_json == {"request_id": str(first.request_id)}
    h.current.id = uuid4()
    with pytest.raises(ReconciliationRequestConflictError):
        await accepted(h)
    # 現在の同じ Project 読取権による歴史確認は、元会話と異なっても新実行を作らない。
    confirmed = await h.service.confirm(
        access=h.access,
        request_id=first.request_id,
        project_id=h.reference.project_id,
        run_id=h.reference.run_id,
    )
    assert confirmed == first


@pytest.mark.parametrize("change", ["csrf", "cookie", "disabled", "role", "expired", "member"])
async def test_accept_rejects_current_authority_before_target_or_outbox(harness, change):
    """元 HTTP credential と所属が失効していれば、対象読取や要求作成をしない。"""
    h = harness
    if change == "csrf":
        h.accept_args["access"] = replace(h.access, csrf_token="wrong")
    elif change == "cookie":
        h.accept_args["access"] = replace(
            h.access, session_token=generate_session_credentials().session_token
        )
    elif change == "disabled":
        h.user.status = "DISABLED"
    elif change == "role":
        h.user.system_role = "ADMIN"
    elif change == "expired":
        h.current.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        h.member.status = "INACTIVE"
    with pytest.raises((UnauthorizedSessionError, CsrfRejectedError, ProjectNotFoundError)):
        await accepted(h)
    h.load.assert_not_awaited()
    with h.database() as (_, session):
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(EffectReconciliationRequest)) == 0
        )
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 0


async def test_accept_flush_expiry_rolls_back_request_and_outbox(harness):
    """flush 待ちで会話が失効した場合、受理/Outbox の片方も残さない。"""
    h = harness
    h.flush_hook = lambda: setattr(
        h.current, "idle_expires_at", datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(UnauthorizedSessionError):
        await accepted(h)
    with h.database() as (_, session):
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 0
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(EffectReconciliationRequest)) == 0
        )


async def test_claim_commit_loss_does_not_reissue_owner_or_read(harness):
    """認領済み応答喪失を新 token で回復せず、期限後は只読要求だけを回収する。"""
    h = harness
    await accepted(h)
    reader = AsyncMock()
    executor = EffectReconciliationExecutor(requests=h.service, reader=reader)
    h.lose_commit = True
    with pytest.raises(OSError):
        await executor.execute(h.original["request_id"])
    assert await executor.execute(h.original["request_id"]) == "not_claimed"
    reader.observe.assert_not_awaited()
    with h.database() as (repo, _):
        row = await repo.require(h.original["request_id"])
        row.created_at -= timedelta(minutes=2)
        row.claimed_at -= timedelta(minutes=2)
        row.lease_expires_at -= timedelta(minutes=2)
    assert await h.service.recover_expired(limit=10) == 1
    assert await h.service.recover_expired(limit=10) == 0
    row = await stored(h)
    assert row.status == "FAILED" and row.error_code == "lookup_interrupted"
    assert row.observation_status is None


@pytest.mark.parametrize("recover", [False, True])
async def test_lost_queue_delivery_expires_without_starting_a_read(harness, recover):
    """認領前に job が失われても active 枠を永久に占有せず、遅配で照会を開始しない。"""
    h = harness
    await accepted(h)
    with h.database() as (repo, _):
        row = await repo.require(h.original["request_id"])
        row.created_at -= timedelta(minutes=31)
    if recover:
        assert await h.service.recover_expired(limit=10) == 1
    else:
        assert await h.service.claim(h.original["request_id"]) is None
    row = await stored(h)
    assert row.status == "FAILED" and row.error_code == "lookup_interrupted"
    assert row.owner_hash is None and row.observation_status is None
    assert await h.service.claim(h.original["request_id"]) is None
    with h.database() as (_, session):
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 1


@pytest.mark.parametrize("change", ["revoked", "member", "target"])
async def test_queued_request_revalidates_current_authority_and_target(harness, change):
    """受理後の変更を Queue identity で上書きせず、読取前に閉じる。"""
    h = harness
    await accepted(h)
    if change == "revoked":
        h.current.revoked_at = datetime.now(UTC)
    elif change == "member":
        h.member.status = "INACTIVE"
    else:
        h.load.return_value = replace(h.original["target"], secret_reference_id=uuid4())
    assert await h.service.claim(h.original["request_id"]) is None
    row = await stored(h)
    assert row.status == ("FAILED" if change == "target" else "REVOKED")
    assert row.owner_hash is None


@pytest.mark.parametrize("change", ["revoked", "expired_owner"])
async def test_finish_flush_loss_rolls_back_observation(harness, change, monkeypatch):
    """結果 flush の後に失権しても SUCCEEDED/receipt を部分 commit しない。"""
    h = harness
    await accepted(h)
    owner = await h.service.claim(h.original["request_id"])
    observed = replace(observation(h.original), observed_at=datetime.now(UTC))
    if change == "revoked":
        h.flush_hook = lambda: setattr(h.current, "revoked_at", datetime.now(UTC))
    else:

        class Later(datetime):
            """flush の後だけ時計を期限後に進める。"""

            @classmethod
            def now(cls, tz=None):
                return datetime.now(UTC) + timedelta(seconds=61)

        h.flush_hook = lambda: monkeypatch.setattr(
            "skillmind.effects.reconciliation_request_service.datetime", Later
        )
    with pytest.raises(EffectReconciliationDeniedError):
        await h.service.finish(owner, observed)
    row = await stored(h)
    assert row.status == ("REVOKED" if change == "revoked" else "FAILED")
    assert row.receipt_json is None and row.observation_status is None


async def test_executor_queries_without_transaction_and_stores_once(harness):
    """実台帳と executor を接続し、外部 I/O は DB lock の外で一回だけ行う。"""
    h = harness
    await accepted(h)

    async def read(reference, *, accepted_target_checksum, authorize_request):
        """外部 port で所有権 callback と元 target checksum の引渡しを検証する。"""
        assert h.open_transactions == 0 and reference == h.reference
        assert accepted_target_checksum == (await stored(h)).target_checksum
        await authorize_request()
        assert h.open_transactions == 0
        return replace(observation(h.original), observed_at=datetime.now(UTC))

    reader = AsyncMock(observe=AsyncMock(side_effect=read))
    executor = EffectReconciliationExecutor(requests=h.service, reader=reader)
    assert await executor.execute(h.original["request_id"]) == "observed"
    assert await executor.execute(h.original["request_id"]) == "not_claimed"
    reader.observe.assert_awaited_once()
    row = await stored(h)
    assert row.status == "SUCCEEDED" and row.observation_status == "NOT_OBSERVED"


async def test_executor_cancellation_preserves_interrupted_lookup(harness):
    """取消を伝播し、読取の清理完了後に停止記録を残す。"""
    h = harness
    await accepted(h)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def read(*args, **kwargs):
        """応答を返さない transport の取消清理を模す。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    executor = EffectReconciliationExecutor(
        requests=h.service, reader=AsyncMock(observe=AsyncMock(side_effect=read))
    )
    task = asyncio.create_task(executor.execute(h.original["request_id"]))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    row = await stored(h)
    assert row.status == "FAILED" and row.error_code == "lookup_interrupted"
    assert row.observation_status is None


async def test_observation_commit_response_loss_preserves_original_result(harness):
    """保存成功後の応答喪失でも原観測を失敗へ上書きせず、再配送で外部照会しない。"""
    h = harness
    await accepted(h)

    async def read(*args, **kwargs):
        """次の finish の実 commit だけを応答喪失にする。"""
        h.lose_commit = True
        return replace(observation(h.original), observed_at=datetime.now(UTC))

    reader = AsyncMock(observe=AsyncMock(side_effect=read))
    executor = EffectReconciliationExecutor(requests=h.service, reader=reader)
    assert await executor.execute(h.original["request_id"]) == "unavailable"
    row = await stored(h)
    assert row.status == "SUCCEEDED" and row.observation_status == "NOT_OBSERVED"
    assert await executor.execute(h.original["request_id"]) == "not_claimed"
    reader.observe.assert_awaited_once()
