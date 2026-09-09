"""専用 PostgreSQL 上の lock/rollback/遅着結算。接続できない場合は未検証として skip する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.db.models import (
    Run,
    RunBudgetAccount,
    RunBudgetReceipt,
    RunBudgetReservation,
    RunEvent,
)
from projectmind.runs.budget import (
    BudgetExecutionRecord,
    BudgetExhaustedError,
    BudgetPolicy,
    BudgetReservationRequest,
    MeteringMode,
)
from projectmind.runs.budget_store import PostgresRunBudgetStore
from projectmind.runs.domain import (
    ClaimedRun,
    LeaseValidationError,
    RunAttemptStatus,
    RunStatus,
    lease_token_hash,
)
from projectmind.runs.repository import RunRepository
from projectmind.runs.repository_budgets import RunBudgetRepository, new_budget_account
from tests.db.test_real_database_invariants import (
    _insert_in_order,
    _run_spine,
    _session_factory,
)
from tests.db.test_real_database_invariants import (
    migrated_database_url as migrated_database_url,
)
from tests.runs.test_repository_budgets import report
from tests.worker.test_agent_run_executor import _claimed


async def seed_budget(factory: async_sessionmaker[AsyncSession]) -> ClaimedRun:
    """実 DB 用の Run/Segment/Attempt と勘定を FK の親順に保存する。"""

    now = datetime.now(UTC)
    run, segment, attempt, primary = _run_spine(now)
    run.status, run.started_at = "QUEUED", None
    run.limits_snapshot_json = {"max_turns": 20, "max_budget_usd": "0.000000200"}
    account = new_budget_account(
        run, BudgetPolicy(20, 200, "fixture-adapter", "fixture/v1", MeteringMode.CUMULATIVE)
    )
    run.status, run.started_at = "RUNNING", now
    claim = replace(
        _claimed(),
        run_id=run.id,
        project_id=run.project_id,
        run_segment_id=segment.id,
        run_attempt_id=attempt.id,
        lease_expires_at=now + timedelta(minutes=5),
        limits_snapshot_json=dict(run.limits_snapshot_json),
    )
    attempt.lease_token_hash = lease_token_hash(claim.lease_token)
    attempt.lease_expires_at = claim.lease_expires_at
    async with factory() as session, session.begin():
        await _insert_in_order(session, run, segment, attempt, primary, account)
    store = PostgresRunBudgetStore(factory)
    await store.reserve_group(
        claim, group_key="primary", requests=(BudgetReservationRequest("primary", 8, 80),)
    )
    assert await store.start_execution(claim, execution_key="primary")
    return claim


async def assert_blocked_by(observer: AsyncSession, *, holder: int, waiter: int) -> None:
    """task が未完了という推測でなく、PostgreSQL の実 blocker を観測する。"""

    async with asyncio.timeout(5):
        while not await observer.scalar(
            text("SELECT :holder = ANY(pg_blocking_pids(:waiter))"),
            {"holder": holder, "waiter": waiter},
        ):
            await asyncio.sleep(0.01)


async def test_postgres_serializes_groups_competing_for_remaining_budget(
    migrated_database_url: str,
) -> None:
    """Run lock の競争を実際に作り、片群だけ成功して残額を超えないことを確認する。"""

    async with _session_factory(migrated_database_url) as factory:
        claim = await seed_budget(factory)
        pids: asyncio.Queue[int] = asyncio.Queue()

        async def compete(key: str) -> tuple[BudgetExecutionRecord, ...]:
            """別 connection の transaction で元 Tool ごとの分支を預留する。"""

            async with factory() as session, session.begin():
                pids.put_nowait(await session.scalar(select(func.pg_backend_pid())))
                return await RunBudgetRepository(session).reserve_group(
                    claim,
                    group_key=key,
                    requests=(BudgetReservationRequest(key, 10, 100, "primary"),),
                )

        tasks: list[asyncio.Task[tuple[BudgetExecutionRecord, ...]]] = []
        try:
            async with factory() as holder, holder.begin(), factory() as observer:
                holder_pid = await holder.scalar(select(func.pg_backend_pid()))
                await holder.scalar(select(Run).where(Run.id == claim.run_id).with_for_update())
                tasks = [asyncio.create_task(compete(key)) for key in ("a", "b")]
                for _ in tasks:
                    waiter = await asyncio.wait_for(pids.get(), 5)
                    await assert_blocked_by(observer, holder=holder_pid, waiter=waiter)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert sum(isinstance(item, BudgetExhaustedError) for item in results) == 1
            assert sum(isinstance(item, tuple) for item in results) == 1
            async with factory() as session:
                account = await session.scalar(
                    select(RunBudgetAccount).where(RunBudgetAccount.run_id == claim.run_id)
                )
                assert account is not None and account.reserved_turns == 18
                assert account.reserved_cost_nanos == 180
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(RunBudgetReservation)
                        .where(RunBudgetReservation.run_id == claim.run_id)
                    )
                    == 2
                )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_postgres_rollback_removes_group_and_balance_together(
    migrated_database_url: str,
) -> None:
    """flush 後に transaction を rollback し、占用だけが残る半端な状態を検出する。"""

    async with _session_factory(migrated_database_url) as factory:
        claim = await seed_budget(factory)
        with pytest.raises(RuntimeError, match="injected rollback"):
            async with factory() as session, session.begin():
                await RunBudgetRepository(session).reserve_group(
                    claim,
                    group_key="rolled-back",
                    requests=(BudgetReservationRequest("child", 5, 50, "primary"),),
                )
                raise RuntimeError("injected rollback")
        async with factory() as session:
            account = await session.scalar(
                select(RunBudgetAccount).where(RunBudgetAccount.run_id == claim.run_id)
            )
            assert account is not None and account.reserved_turns == 8
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunBudgetReservation)
                    .where(RunBudgetReservation.run_id == claim.run_id)
                )
                == 1
            )


async def test_postgres_late_settlement_does_not_reopen_terminal_run(
    migrated_database_url: str,
) -> None:
    """Run の本当の終態 transaction 後も独立勘定へだけ結算する。"""

    async with _session_factory(migrated_database_url) as factory:
        claimed = await seed_budget(factory)
        async with factory() as session, session.begin():
            await RunRepository(session).finalize_execution(
                claimed,
                target=RunStatus.FAILED,
                attempt_status=RunAttemptStatus.FAILED,
                event=None,
                session_metadata=None,
                result=None,
                error_json={"code": "fixture_failure"},
            )
        store = PostgresRunBudgetStore(factory)
        claim = await store.claim_reconciliation(
            project_id=claimed.project_id,
            run_id=claimed.run_id,
            execution_key="primary",
            worker_id="budget-test",
            token=str(uuid4()),
        )
        await store.record_usage(claim, report("final", 3, 30, final=True))
        await store.confirm_stopped(
            claim, receipt_key="stop", verified_evidence="fixture/verified-stop"
        )
        assert (
            await store.record_usage(claim, report("final", 3, 30, final=True))
        ).disposition == "REPLAY"
        with pytest.raises(LeaseValidationError):
            await store.start_execution(claimed, execution_key="primary")
        async with factory() as session:
            run = await session.get(Run, claimed.run_id)
            assert run is not None and run.status == "FAILED"
            events = list(
                (
                    await session.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == claimed.run_id)
                        .order_by(RunEvent.sequence)
                    )
                ).all()
            )
            assert len(events) == 1 and events[0].event_type == "RUN_SNAPSHOT"
            account = await session.scalar(
                select(RunBudgetAccount).where(RunBudgetAccount.run_id == claimed.run_id)
            )
            assert (
                account is not None and account.consumed_turns == 3 and account.reserved_turns == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RunBudgetReceipt)
                    .where(RunBudgetReceipt.reservation_id == claim.reservation_id)
                )
                == 2
            )
