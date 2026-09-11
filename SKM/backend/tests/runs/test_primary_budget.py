"""主実行の初回帳簿と残額予約を実 service/store と transaction fake で検証する。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from skillmind.db.models import Run, RunBudgetAccount
from skillmind.runs.budget import BudgetPolicy, BudgetUnavailableError, MeteringMode
from skillmind.runs.budget_store import PostgresRunBudgetStore
from skillmind.runs.service import RunService
from tests.runs.budget_fakes import BudgetDatabase
from tests.runs.creation_authorization_harness import CreationAuthorizationHarness, row_values
from tests.runs.test_budget_store import BudgetSessions

POLICY = BudgetPolicy(12, None, "fixture-adapter", "fixture/v1", MeteringMode.CUMULATIVE)


@pytest.mark.asyncio
async def test_new_run_and_account_commit_together() -> None:
    """保存済み Run の再構築ではなく、新規 INSERT 勝者の同 transaction だけで作る。"""
    db = CreationAuthorizationHarness()
    db.service = RunService(db.session_factory, budget_policy=POLICY)
    created = await db.call()
    run = next(row for row in db.committed if isinstance(row, Run))
    account = next(row for row in db.committed if isinstance(row, RunBudgetAccount))
    assert created is not None and account.run_id == run.id == created.run_id
    assert run.limits_snapshot_json["max_turns"] == 12
    assert account.policy_json == POLICY.to_json()
    assert account.consumed_turns == account.reserved_turns == 0
    assert db.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_creation_never_commits_a_detached_account(committed: bool) -> None:
    """commit の応答を失っても Run と帳簿の保存事実は同じで、勝手に再作成しない。"""
    db = CreationAuthorizationHarness()
    db.commit_unknown = committed
    db.service = RunService(db.session_factory, budget_policy=POLICY)
    with pytest.raises(ConnectionError):
        await db.call()
    runs = [row for row in db.committed if isinstance(row, Run)]
    accounts = [row for row in db.committed if isinstance(row, RunBudgetAccount)]
    assert len(runs) == len(accounts) == int(committed)
    if committed:
        assert accounts[0].run_id == runs[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["first-replay", "unique-winner"])
async def test_existing_run_never_gets_a_zero_account_on_replay(path: str) -> None:
    """配備 policy が追加されても、旧 Run の履歴/限額を書き換えない。"""
    db = CreationAuthorizationHarness(path)
    original = row_values(db.winner)
    db.service = RunService(db.session_factory, budget_policy=POLICY)
    created = await db.call()
    assert created is not None and created.idempotent_replay
    assert row_values(db.winner) == original
    assert not any(isinstance(row, RunBudgetAccount) for row in db.committed)


def test_cost_policy_is_not_silently_downgraded_to_turns_only() -> None:
    """金銭 adapter 未接続時に、設定された費用限額を無視して実行可能にしない。"""
    db = CreationAuthorizationHarness()
    with pytest.raises(BudgetUnavailableError, match="cost adapter"):
        RunService(db.session_factory, budget_policy=replace(POLICY, max_cost_nanos=100))


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [False, True])
async def test_primary_reserves_only_uncommitted_balance_and_replays_original(cost: bool) -> None:
    """停止済みだが未結算の占用を保持し、同主 Attempt の重放は再計算しない。"""
    db = BudgetDatabase(cost=cost)
    await db.reserve(turns=8, cost=80 if cost else None)
    await db.start()
    claim = await db.reconciler()
    await db.repository.confirm_stopped(
        claim, receipt_key="stopped", verified_evidence="fixture:stopped"
    )
    store = PostgresRunBudgetStore(BudgetSessions(db))  # type: ignore[arg-type]
    original = await store.reserve_primary(db.claimed, expected_policy=db.policy)
    replay = await store.reserve_primary(db.claimed, expected_policy=db.policy)
    assert original == replay
    assert original.granted_turns == 12
    assert original.granted_cost_nanos == (120 if cost else None)
    assert db.account.reserved_turns == 20 and len(db.reservations) == 2
    assert db.account.consumed_turns == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_primary_lost_commit_confirms_original_without_reallocating(committed: bool) -> None:
    """A の確認だけを行い、元預留が無ければ残額が十分でも新規起動へ進まない。"""
    db = BudgetDatabase(cost=False)
    sessions = BudgetSessions(db, fail_on=1, committed=committed)
    store = PostgresRunBudgetStore(sessions)  # type: ignore[arg-type]
    if committed:
        result = await store.reserve_primary(db.claimed, expected_policy=db.policy)
        assert result.granted_turns == 20 and len(db.reservations) == 1
    else:
        with pytest.raises(BudgetUnavailableError, match="not confirmed"):
            await store.reserve_primary(db.claimed, expected_policy=db.policy)
        assert not db.reservations and db.account.reserved_turns == 0
    assert len(sessions.sessions) == 2


@pytest.mark.asyncio
async def test_primary_rejects_wrong_adapter_without_reservation() -> None:
    """限額が一致しても計量 profile の意味が違う coordinator を受け付けない。"""
    db = BudgetDatabase(cost=False)
    store = PostgresRunBudgetStore(BudgetSessions(db))  # type: ignore[arg-type]
    with pytest.raises(BudgetUnavailableError, match="primary adapter"):
        await store.reserve_primary(db.claimed, expected_policy=replace(db.policy, source="other"))
    assert not db.reservations and db.account.reserved_turns == Decimal(0)
