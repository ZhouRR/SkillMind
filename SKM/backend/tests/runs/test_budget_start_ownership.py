"""原記述子の再利用を新しい起動所有権と誤認しない。実 DB・課金は行わない。"""

from __future__ import annotations

import asyncio
import os
import pickle
from copy import copy, deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.domain import AgentEvent
from skillmind.agent.metering import AgentInvocation
from skillmind.core.cancellation import check_pending_cancellation
from skillmind.core.hashing import canonical_json
from skillmind.db.models import RunBudgetReservation
from skillmind.runs.budget import (
    BudgetError,
    BudgetStartUncertainError,
    BudgetUnavailableError,
    budget_start_owner_hash,
)
from skillmind.runs.budget_store import PostgresRunBudgetStore
from tests.agent.test_budget_start_boundary import _collect, _engine
from tests.agent.test_budget_start_boundary import (
    isolated_sdk_environment as isolated_sdk_environment,
)
from tests.runs.budget_fakes import START_OWNER_TOKEN, BudgetDatabase, budget_invocation
from tests.runs.test_budget_execution import LedgerExecution
from tests.runs.test_budget_observations import balances, raw_observation
from tests.runs.test_budget_store import BudgetSessions


def reservation_values(row: RunBudgetReservation) -> dict[str, Any]:
    """ORM identity を除いた保存値を採り、拒否で元の記録が書き換わらないか確認する。"""

    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def budget_store(db: BudgetDatabase) -> tuple[PostgresRunBudgetStore, BudgetSessions]:
    """既存 transaction fake へ型境界だけを置き、repository の処理は差し替えない。"""

    sessions = BudgetSessions(db)
    return PostgresRunBudgetStore(cast(async_sessionmaker[AsyncSession], sessions)), sessions


@pytest.mark.parametrize("committed", [False, True])
async def test_new_recorder_cannot_restart_original_binding_after_unknown_start(
    tmp_path: Path,
    committed: bool,
) -> None:
    """B の未 commit を試験側が知っていても、新調整者へ再起動許可を発行しない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    case.sessions.fail_on, case.sessions.commit_before_failure = 3, committed
    with pytest.raises(BudgetStartUncertainError):
        await case.execute()
    row = case.database.reservations[0]
    assert row.status == ("START_INTENT" if committed else "RESERVED")
    assert row.invocation_json is not None
    assert not case.factory.clients

    prepared = AgentInvocation.from_json(row.invocation_json)
    original_binding = (row.invocation_id, row.invocation_checksum, row.invocation_json)
    recorder = case.new_recorder()
    engine = _engine(case.factory, recorder.observe_result, recorder.before_connect)
    events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(engine.execute(replace(case.context, prepared_invocation=prepared)), events)

    assert (row.invocation_id, row.invocation_checksum, row.invocation_json) == original_binding
    assert row.status == ("START_INTENT" if committed else "RESERVED")
    assert len(case.sessions.sessions) == 4
    assert not case.factory.clients and not events and not case.database.observations
    case.assert_held()


@pytest.mark.parametrize("started", [False, True], ids=["reserved", "start-intent"])
@pytest.mark.parametrize("operation", ["bind", "confirm", "start"])
async def test_different_owner_cannot_confirm_binding_or_use_start_permission(
    started: bool,
    operation: str,
) -> None:
    """同じ lease/記述子を知る別所有者も、原 B の呼出権を借用できない。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    await db.bind()
    if started:
        await db.start()
    row = db.reservations[0]
    original = reservation_values(row)
    invocation = AgentInvocation.from_json(row.invocation_json)
    db.session.flush.reset_mock()
    with pytest.raises(BudgetUnavailableError) as caught:
        if operation == "start":
            values = {**db.bound_arguments(), "start_owner_token": "B" * 43}
            await db.repository.start_execution(db.claimed, execution_key="primary", **values)
        else:
            await db.repository.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token="B" * 43,
                confirm_only=operation == "confirm",
            )
    assert "B" * 43 not in str(caught.value)
    assert reservation_values(row) == original
    db.session.flush.assert_not_awaited()
    assert not db.receipts and not db.observations


@pytest.mark.parametrize("damage", [None, "invalid", "sha256:" + "A" * 64, "sha256:" + "0" * 64])
@pytest.mark.parametrize("operation", ["bind", "confirm", "start"])
async def test_missing_or_corrupt_owner_is_not_recovered_from_valid_descriptor(
    damage: str | None,
    operation: str,
) -> None:
    """旧束縛や破損 hash は正しい descriptor から補填せず、保存されたまま拒否する。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    await db.bind()
    row = db.reservations[0]
    row.invocation_start_owner_hash = damage
    original = reservation_values(row)
    invocation = AgentInvocation.from_json(row.invocation_json)
    store, sessions = budget_store(db)
    with pytest.raises(BudgetUnavailableError):
        if operation == "start":
            await store.start_execution(db.claimed, execution_key="primary", **db.bound_arguments())
        elif operation == "bind":
            await store.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token=START_OWNER_TOKEN,
            )
        else:
            await db.repository.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token=START_OWNER_TOKEN,
                confirm_only=True,
            )
    assert reservation_values(row) == original
    for session in sessions.sessions:
        session.flush.assert_not_awaited()
    assert row.status == "RESERVED" and row.start_intent_at is None


async def test_same_owner_binding_confirmation_preserves_original_hashes_and_one_start() -> None:
    """同じ所有者の束縛確認は書込を増やさず、B は初回だけ True を返す。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    row = db.reservations[0]
    old_hashes = (row.group_checksum, row.request_checksum, db.account.policy_checksum)
    binding = await db.bind()
    original = reservation_values(row)
    assert row.invocation_start_owner_hash == budget_start_owner_hash(START_OWNER_TOKEN)
    assert all(
        START_OWNER_TOKEN not in value for value in original.values() if isinstance(value, str)
    )
    assert START_OWNER_TOKEN not in canonical_json(row.invocation_json)
    db.session.flush.reset_mock()
    assert await db.bind() == binding
    assert reservation_values(row) == original
    db.session.flush.assert_not_awaited()
    assert await db.repository.start_execution(
        db.claimed, execution_key="primary", **db.bound_arguments()
    )
    assert not await db.repository.start_execution(
        db.claimed, execution_key="primary", **db.bound_arguments()
    )
    assert (row.group_checksum, row.request_checksum, db.account.policy_checksum) == old_hashes
    assert row.reserved_turns == db.account.reserved_turns == 8


async def test_owner_hash_without_invocation_cannot_be_rebound() -> None:
    """部分行の owner だけを見て、新しい記述子を安全な初回として追加しない。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    row = db.reservations[0]
    row.invocation_start_owner_hash = budget_start_owner_hash(START_OWNER_TOKEN)
    original = reservation_values(row)
    with pytest.raises(BudgetUnavailableError):
        await db.bind()
    assert reservation_values(row) == original


@pytest.mark.parametrize(
    "token", ["", "A" * 42, "A" * 44, "A" * 42 + "\n", "A" * 42 + "/", None, 7]
)
@pytest.mark.parametrize("operation", ["bind", "start"])
async def test_invalid_owner_token_is_rejected_without_sql(token: object, operation: str) -> None:
    """不正な形を便利変換したり、原 token/hash を例外へ含めたりしない。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    await db.bind()
    invocation = budget_invocation(db.claimed)
    db.session.scalars.reset_mock()
    with pytest.raises(BudgetError):
        if operation == "bind":
            await db.repository.bind_invocation(
                db.claimed,
                execution_key="primary",
                invocation=invocation,
                start_owner_token=cast(str, token),
            )
        else:
            values = {**db.bound_arguments(), "start_owner_token": token}
            await db.repository.start_execution(db.claimed, execution_key="primary", **values)
    db.session.scalars.assert_not_awaited()


async def test_released_binding_denies_even_original_start_owner() -> None:
    """原 token は永続状態を上書きする権限ではなく、未起動閉鎖後も B を拒否する。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    await db.bind()
    claim = await db.reconciler()
    await db.repository.release_unstarted(claim, receipt_key="fixture-release")
    row = db.reservations[0]
    original = reservation_values(row)
    with pytest.raises(BudgetUnavailableError):
        await db.repository.start_execution(
            db.claimed, execution_key="primary", **db.bound_arguments()
        )
    assert row.status == "RELEASED" and reservation_values(row) == original
    assert db.account.reserved_turns == 0


async def test_recorder_uses_distinct_one_shot_tokens_without_leaking_them_to_sdk_or_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有者の乱数を最初の await 前に消費し、descriptor/SDK/公開 event へ渡さない。"""

    generated = iter(("C" * 43, "D" * 43))
    entropy_requests: list[int] = []

    def token_urlsafe(size: int) -> str:
        """乱数源の要求 bit 数を確認するためだけの合成値で、保存から復元しない。"""

        entropy_requests.append(size)
        return next(generated)

    monkeypatch.setattr("skillmind.runs.budget_execution.secrets.token_urlsafe", token_urlsafe)
    case = LedgerExecution(tmp_path)
    second = case.new_recorder()
    original_token = case.recorder._start_owner_token
    other_token = second._start_owner_token
    assert original_token != other_token
    assert entropy_requests == [32, 32]
    assert original_token is not None and other_token is not None
    active_recorder = case.recorder
    checkpoints: list[str | None] = []

    async def check() -> None:
        """callback が最初に待機し得る入口で既に所有 token が消費済みか確認する。"""

        checkpoints.append(active_recorder._start_owner_token)
        await check_pending_cancellation()

    monkeypatch.setattr("skillmind.runs.budget_execution.check_pending_cancellation", check)
    await case.reserve()
    await case.execute()
    row = case.database.reservations[0]
    assert row.invocation_start_owner_hash == budget_start_owner_hash(original_token)
    assert row.invocation_start_owner_hash != budget_start_owner_hash(other_token)
    prepared = AgentInvocation.from_json(row.invocation_json)
    public_values = (
        canonical_json(row.invocation_json),
        canonical_json(case.database.observations[0].payload_json),
        repr(case.factory.clients[0].options),
        repr(case.recorder._binding),
        repr(case.events),
    )
    assert all(
        secret not in value
        for secret in (original_token, other_token, row.invocation_start_owner_hash)
        for value in public_values
    )
    with pytest.raises(BudgetUnavailableError, match="already been used"):
        await case.recorder.before_connect(prepared)
    active_recorder = second
    with pytest.raises(BudgetUnavailableError):
        await second.before_connect(prepared)
    with pytest.raises(BudgetUnavailableError, match="already been used"):
        await second.before_connect(prepared)
    assert checkpoints and all(value is None for value in checkpoints)
    assert case.recorder._start_owner_token is None and second._start_owner_token is None
    assert len(case.factory.clients) == len(case.database.observations) == 1
    case.assert_held()


async def test_same_tick_recorder_reentry_is_rejected_before_first_binding_commit(
    tmp_path: Path,
) -> None:
    """原 bind の応答待ち中も、同じ callback を二つの B へ分岐させない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    entered, release = asyncio.Event(), asyncio.Event()
    case.sessions.commit_entered, case.sessions.commit_release = entered, release
    first = asyncio.create_task(case.execute())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert case.recorder._start_owner_token is None
        assert len(case.sessions.sessions) == 2 and not case.factory.clients
        invocation = AgentInvocation.from_json(case.database.reservations[0].invocation_json)
        with pytest.raises(BudgetUnavailableError, match="already been used"):
            await case.recorder.before_connect(invocation)
        assert len(case.sessions.sessions) == 2
        release.set()
        await asyncio.wait_for(first, timeout=2)
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    assert len(case.factory.clients) == len(case.database.observations) == 1
    case.assert_held()


@pytest.mark.parametrize("terminal", [False, True])
async def test_legacy_bound_start_without_owner_retains_independent_observation_audit(
    terminal: bool,
) -> None:
    """旧 owner 不明は再起動拒否であり、保存済み実行の核対・観測監査の削除ではない。"""

    db = BudgetDatabase(cost=False)
    await db.reserve()
    await db.start()
    row = db.reservations[0]
    row.invocation_start_owner_hash = None
    invocation = AgentInvocation.from_json(row.invocation_json)
    store, _sessions = budget_store(db)
    with pytest.raises(BudgetUnavailableError):
        await store.bind_invocation(
            db.claimed,
            execution_key="primary",
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )
    with pytest.raises(BudgetUnavailableError):
        await store.start_execution(db.claimed, execution_key="primary", **db.bound_arguments())
    if terminal:
        db.run.status = "CANCELLED"
    previous = balances(db)
    claim = await store.claim_reconciliation(
        project_id=db.run.project_id,
        run_id=db.run.id,
        execution_key="primary",
        worker_id="fixture-legacy-reconciler",
        token="fixture-reconciliation-only",
    )
    observation = raw_observation(db)
    assert (await store.record_observation(claim, observation)).disposition == "OBSERVED"
    assert (await store.record_observation(claim, observation)).disposition == "REPLAY"
    assert balances(db) == previous and row.invocation_start_owner_hash is None
    assert row.invocation_json == invocation.to_json()
    assert len(db.observations) == 1 and not db.receipts


@pytest.mark.parametrize("operation", ["copy", "deepcopy", "pickle"])
async def test_copying_or_serializing_recorder_does_not_duplicate_original_start_owner(
    tmp_path: Path,
    operation: str,
) -> None:
    """通常の copy/pickle で一回限りの所有権を複製せず、元の正規実行は残す。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    original_token = case.recorder._start_owner_token
    assert original_token is not None
    with pytest.raises(BudgetUnavailableError) as caught:
        if operation == "copy":
            copy(case.recorder)
        elif operation == "deepcopy":
            deepcopy(case.recorder)
        else:
            pickle.dumps(case.recorder)
    assert original_token not in str(caught.value)
    assert case.recorder._start_owner_token == original_token
    assert len(case.sessions.sessions) == 1 and not case.factory.clients
    await case.execute()
    assert len(case.factory.clients) == len(case.database.observations) == 1
    assert case.recorder._start_owner_token is None
    case.assert_held()


async def test_changed_process_identity_burns_inherited_recorder_without_sql_or_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 fork はせず PID だけ変え、継承された token を元 process へ戻しても再使用しない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    original_pid = os.getpid()
    with monkeypatch.context() as changes:
        changes.setattr("skillmind.runs.budget_execution.os.getpid", lambda: original_pid + 1)
        with pytest.raises(BudgetUnavailableError):
            await case.execute()
    assert case.recorder._start_owner_token is None
    assert len(case.sessions.sessions) == 1 and not case.factory.clients and not case.events
    assert case.database.reservations[0].invocation_id is None
    with pytest.raises(BudgetUnavailableError):
        await case.execute()
    assert len(case.sessions.sessions) == 1 and not case.factory.clients and not case.events
    case.assert_held()
