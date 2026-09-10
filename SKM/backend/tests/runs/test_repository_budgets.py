"""実 repository と明示 fake DB で、共通残額・核対・元実行の不変条件を検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from skillmind.runs.budget import (
    BudgetConflictError,
    BudgetError,
    BudgetExhaustedError,
    BudgetReservationRequest,
    BudgetUnavailableError,
    BudgetUsageReport,
    MeteringMode,
)
from skillmind.runs.domain import LeaseValidationError, RunCancellationRequestedError
from skillmind.runs.repository_budgets import new_budget_account
from tests.runs.budget_fakes import BudgetDatabase


def report(
    key: str,
    turns: int | None,
    cost: int | None,
    *,
    watermark: int = 1,
    final: bool = False,
    mode: MeteringMode = MeteringMode.CUMULATIVE,
) -> BudgetUsageReport:
    """実 SDK の意味を仮定しない、正規化契約用 fixture を作る。"""

    return BudgetUsageReport(
        key,
        "fixture-adapter",
        "fixture/v1",
        mode,
        turns,
        cost,
        watermark if mode is MeteringMode.CUMULATIVE else None,
        final,
    )


async def test_parent_and_children_share_the_uncommitted_balance() -> None:
    """親の上界を子へ複製せず、二回目の分派も同じ残額から確保する。"""

    db = BudgetDatabase()
    await db.reserve(turns=8, cost=80)
    await db.start()
    children = (
        BudgetReservationRequest("a", 5, 50, "primary"),
        BudgetReservationRequest("b", 5, 50, "primary"),
    )
    first = await db.repository.reserve_group(db.claimed, group_key="tool-1", requests=children)
    assert db.account.reserved_turns == 18
    assert db.account.reserved_cost_nanos == 180
    assert (
        await db.repository.reserve_group(
            db.claimed, group_key="tool-1", requests=tuple(reversed(children))
        )
        == first
    )
    with pytest.raises(BudgetExhaustedError):
        await db.repository.reserve_group(
            db.claimed,
            group_key="tool-2",
            requests=(BudgetReservationRequest("c", 3, 30, "primary"),),
        )
    assert len(db.reservations) == 3
    assert db.account.reserved_turns == 18
    assert db.lock_order[:5] == [
        "runs",
        "run_segments",
        "run_attempts",
        "run_budget_accounts",
        "run_budget_reservations",
    ]


async def test_group_preflight_cannot_leave_partial_reservations() -> None:
    """一支だけなら収まっても、全体不足なら一行も新設しない。"""

    db = BudgetDatabase()
    await db.reserve(turns=10, cost=100)
    await db.start()
    with pytest.raises(BudgetExhaustedError):
        await db.repository.reserve_group(
            db.claimed,
            group_key="too-large",
            requests=(
                BudgetReservationRequest("a", 5, 50, "primary"),
                BudgetReservationRequest("b", 6, 60, "primary"),
            ),
        )
    assert len(db.reservations) == 1
    assert db.account.reserved_turns == 10


async def test_reservation_replay_conflicts_with_different_parameters_or_attempt() -> None:
    """同じ元操作の増額や接管を、残額の再発行に使わせない。"""

    db = BudgetDatabase()
    await db.reserve()
    for request in (
        BudgetReservationRequest("primary", 9, 80),
        BudgetReservationRequest("other", 8, 80),
    ):
        with pytest.raises(BudgetConflictError):
            await db.repository.reserve_group(db.claimed, group_key="primary", requests=(request,))
    db.claimed = replace(db.claimed, run_attempt_id=uuid4())
    db.attempt.id = db.claimed.run_attempt_id
    with pytest.raises(BudgetConflictError):
        await db.reserve()
    assert db.account.reserved_turns == 8


@pytest.mark.parametrize("invalid", ["lease", "budget_lock_expiry", "project", "cancel"])
async def test_all_budget_locks_precede_the_final_authorization(invalid: str) -> None:
    """勘定 lock で待つ間に失効した lease と保存済み取消を拒否する。"""

    db = BudgetDatabase()
    if invalid == "lease":
        db.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid == "budget_lock_expiry":
        db.expire_during_budget_lock = True
    elif invalid == "project":
        db.claimed = replace(db.claimed, project_id=uuid4())
    else:
        db.session.scalar.return_value = uuid4()
    with pytest.raises(
        RunCancellationRequestedError if invalid == "cancel" else LeaseValidationError
    ):
        await db.reserve()
    assert not db.reservations
    assert db.account.reserved_turns == 0


async def test_start_intent_replay_does_not_authorize_a_second_launch() -> None:
    """B の結果不明時、読戻せる意図はモデルの再起動許可ではない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    assert not await db.repository.start_execution(
        db.claimed, execution_key="primary", **db.bound_arguments()
    )
    claim = await db.reconciler()
    with pytest.raises(BudgetError):
        await db.repository.release_unstarted(claim, receipt_key="no-start")
    assert db.account.reserved_turns == 8


async def test_unstarted_release_closes_the_start_gate_and_is_idempotent() -> None:
    """まだ始まれない実行だけを閉じ、後から同じ鍵を再起動できない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.bind()
    claim = await db.reconciler()
    await db.repository.release_unstarted(claim, receipt_key="closed")
    replay = await db.repository.release_unstarted(claim, receipt_key="closed")
    assert replay.disposition == "REPLAY"
    assert db.account.reserved_turns == 0
    assert len(db.receipts) == 1
    with pytest.raises(BudgetUnavailableError):
        await db.repository.start_execution(
            db.claimed, execution_key="primary", **db.bound_arguments()
        )


@pytest.mark.parametrize("stop_first", [False, True])
async def test_settlement_requires_both_stopping_and_complete_usage(stop_first: bool) -> None:
    """最終報告と停止根拠の順が逆でも、一度だけ未使用分を解放する。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    if stop_first:
        await db.repository.confirm_stopped(
            claim, receipt_key="stop", verified_evidence="adapter/stop-1"
        )
        assert db.account.reserved_turns == 8
    await db.repository.record_usage(claim, report("last", 3, 25, final=True))
    assert db.account.consumed_turns == 3
    assert db.account.reserved_turns == (0 if stop_first else 5)
    await db.repository.confirm_stopped(
        claim, receipt_key="stop", verified_evidence="adapter/stop-1"
    )
    assert db.account.reserved_turns == db.account.reserved_cost_nanos == 0
    assert db.account.consumed_cost_nanos == 25
    assert db.reservations[0].status == "SETTLED"
    replay = await db.repository.record_usage(claim, report("last", 3, 25, final=True))
    assert replay.disposition == "REPLAY"
    assert len(db.receipts) == 2


async def test_partial_usage_never_refunds_unknown_cost() -> None:
    """turns だけの最終通知では欠測 cost と残り turns を解放しない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    await db.repository.confirm_stopped(
        claim, receipt_key="stop", verified_evidence="adapter/stopped"
    )
    result = await db.repository.record_usage(claim, report("partial", 3, None, final=True))
    assert result.execution.reserved_turns == 5
    assert result.execution.reserved_cost_nanos == 80
    assert not result.execution.final_usage_confirmed


async def test_ledger_arithmetic_is_independent_of_decimal_context() -> None:
    """他 module の Decimal context が nano-USD の勘定を丸めない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    with localcontext() as context:
        context.prec = 1
        result = await db.repository.record_usage(claim, report("exact", 3, 25))
    assert result.account.consumed_cost_nanos == 25
    assert result.account.reserved_cost_nanos == 55


async def test_cumulative_watermarks_are_per_dimension_and_old_reports_do_not_refund() -> None:
    """先に届いた turns が、遅れた別次元の有効 cost 報告を消さない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    await db.repository.record_usage(claim, report("turns", 5, None, watermark=3))
    await db.repository.record_usage(claim, report("cost", None, 20, watermark=1))
    await db.repository.record_usage(claim, report("old", 2, 10, watermark=0))
    assert db.account.consumed_turns == 5
    assert db.account.consumed_cost_nanos == 20
    assert db.account.block_code is None
    await db.repository.record_usage(claim, report("regression", 4, 20, watermark=4))
    assert db.account.block_code == "cumulative_usage_conflict"
    assert db.account.consumed_turns == 5


async def test_incremental_receipts_are_counted_once() -> None:
    """増分の異なる鍵だけを加算し、同鍵異内容は監査と勘定停止として保存する。"""

    db = BudgetDatabase(mode=MeteringMode.INCREMENTAL)
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    first = report("one", 2, 10, mode=MeteringMode.INCREMENTAL)
    await db.repository.record_usage(claim, first)
    await db.repository.record_usage(claim, first)
    await db.repository.record_usage(claim, report("two", 1, 5, mode=MeteringMode.INCREMENTAL))
    assert db.account.consumed_turns == 3
    result = await db.repository.record_usage(claim, replace(first, turns=7))
    assert result.disposition == "CONFLICT"
    assert db.account.block_code == "receipt_conflict"
    assert db.account.consumed_turns == 3
    assert len(db.receipts) == 3
    with pytest.raises(BudgetUnavailableError):
        await db.reserve("next", turns=1, cost=1)


async def test_overspend_preserves_actual_usage_and_blocks_new_execution() -> None:
    """授与量や Run 上限へ数値を切り詰めて、超過を隠さない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    result = await db.repository.record_usage(claim, report("over", 25, 250, final=True))
    assert result.account.remaining_turns == -5
    assert result.account.remaining_cost_nanos == -50
    assert result.execution.consumed_turns == 25
    assert result.disposition == "OVERSPENT"
    assert db.account.reserved_turns == 0
    with pytest.raises(BudgetUnavailableError):
        await db.reserve("again", turns=1, cost=1)


async def test_terminal_run_and_expired_attempt_do_not_discard_verified_late_usage() -> None:
    """独立核対権で原預留を結算し、旧 Worker へ実行権を返さず RunEvent も追加しない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    db.run.status = "CANCELLED"
    db.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    claim = await db.reconciler()
    await db.repository.record_usage(claim, report("late", 3, 30, final=True))
    await db.repository.confirm_stopped(claim, receipt_key="stop", verified_evidence="adapter/stop")
    assert db.account.consumed_turns == 3
    assert db.account.reserved_turns == 0
    assert db.run.status == "CANCELLED"
    with pytest.raises(LeaseValidationError):
        await db.repository.start_execution(
            db.claimed, execution_key="primary", **db.bound_arguments()
        )
    with pytest.raises(LeaseValidationError):
        await db.repository.record_usage(db.claimed, report("old-token", 4, 40))  # type: ignore[arg-type]


async def test_expired_reconciler_cannot_release_after_takeover() -> None:
    """同じ RunAttempt とは独立に、核対の古い token も再利用させない。"""

    db = BudgetDatabase()
    await db.reserve()
    old = await db.reconciler()
    db.reservations[0].reconcile_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    new = await db.reconciler(worker="reconciler-next")
    with pytest.raises(LeaseValidationError):
        await db.repository.release_unstarted(old, receipt_key="stale")
    await db.repository.release_unstarted(new, receipt_key="current")
    assert len(db.receipts) == 1


async def test_unknown_account_or_tampered_totals_never_recreate_full_credit() -> None:
    """旧 Run の勘定欠落と勘定/明細の不整合を fail closed にする。"""

    db = BudgetDatabase()
    db.account.reserved_turns = Decimal(1)
    with pytest.raises(BudgetUnavailableError):
        await db.reserve()
    db.account = None  # type: ignore[assignment]
    with pytest.raises(BudgetUnavailableError):
        await db.reserve()
    assert not db.reservations


def test_new_account_factory_rejects_running_or_incompatible_limits() -> None:
    """既存実行を初期化して消費履歴を洗う入口を作らない。"""

    db = BudgetDatabase()
    with pytest.raises(BudgetError):
        new_budget_account(db.run, db.policy)
    db.run.status = "QUEUED"
    with pytest.raises(BudgetError):
        new_budget_account(db.run, replace(db.policy, max_turns=21))
