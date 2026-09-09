"""A/B/C の commit 応答喪失と返却時点を、独立 session/明示 transaction fake で検証する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.runs.budget import (
    BudgetReservationRequest,
    BudgetStartUncertainError,
    BudgetUnavailableError,
)
from projectmind.runs.budget_store import PostgresRunBudgetStore
from tests.runs.budget_fakes import BudgetDatabase
from tests.runs.test_repository_budgets import report


class BudgetTransaction:
    """commit/rollback と応答喪失を区別し、fake 永続状態にも反映する。"""

    def __init__(self, factory: BudgetSessions, number: int) -> None:
        """各 transaction 開始時の行値を snapshot する。"""

        self.factory, self.number = factory, number
        db = factory.database
        self.old_reservations, self.old_receipts = list(db.reservations), list(db.receipts)
        self.old_observations = list(db.observations)
        self.old_values = [
            (
                row,
                {
                    column.name: deepcopy(getattr(row, column.name))
                    for column in row.__table__.columns
                },
            )
            for row in [db.account, *db.reservations, *db.receipts, *db.observations]
        ]

    async def __aenter__(self) -> Self:
        """service が所有する transaction を開始する。"""

        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: object
    ) -> None:
        """応答を失っても commit 済みの場合と、実際に rollback した場合を分ける。"""

        del error, traceback
        fail = self.number == self.factory.fail_on
        if self.factory.commit_entered is not None and kind is None:
            self.factory.commit_entered.set()
            assert self.factory.commit_release is not None
            await self.factory.commit_release.wait()
        if kind is not None or (fail and not self.factory.commit_before_failure):
            self.factory.database.reservations[:] = self.old_reservations
            self.factory.database.receipts[:] = self.old_receipts
            self.factory.database.observations[:] = self.old_observations
            for row, values in self.old_values:
                for name, value in values.items():
                    setattr(row, name, value)
        if fail and kind is None:
            raise ConnectionError("commit response lost")


class BudgetSessions:
    """同じ DB の事実を使うが、各 call には別の session を渡す。"""

    def __init__(
        self, database: BudgetDatabase, *, fail_on: int | None = None, committed: bool = True
    ) -> None:
        """失敗箇所と commit の実際の成否を独立して注入する。"""

        self.database, self.fail_on, self.commit_before_failure = database, fail_on, committed
        self.sessions: list[MagicMock] = []
        self.commit_entered: asyncio.Event | None = None
        self.commit_release: asyncio.Event | None = None

    def __call__(self) -> MagicMock:
        """同一 identity map を再利用した確認を偽装しない。"""

        session = MagicMock(spec=AsyncSession)
        session.scalars = AsyncMock(side_effect=self.database.scalars)
        session.scalar = AsyncMock(return_value=None)
        session.add.side_effect = self.database.add
        session.__aenter__.return_value = session
        self.sessions.append(session)
        session.begin.return_value = BudgetTransaction(self, len(self.sessions))
        return session


@pytest.mark.parametrize("committed", [False, True])
async def test_reservation_lost_response_confirms_original_group_without_reallocating(
    committed: bool,
) -> None:
    """A の確認は read-only モードで行い、未 commit の要求を新たに成立させない。"""

    db = BudgetDatabase()
    factory = BudgetSessions(db, fail_on=1, committed=committed)
    store = PostgresRunBudgetStore(factory)  # type: ignore[arg-type]
    arguments: dict[str, Any] = {
        "group_key": "operation",
        "requests": (BudgetReservationRequest("primary", 8, 80),),
    }
    if committed:
        records = await store.reserve_group(db.claimed, **arguments)
        assert records[0].granted_turns == 8
        assert db.account.reserved_turns == 8
        assert len(db.reservations) == 1
    else:
        with pytest.raises(BudgetUnavailableError, match="not committed"):
            await store.reserve_group(db.claimed, **arguments)
        assert not db.reservations
        assert db.account.reserved_turns == 0
    assert len(factory.sessions) == 2
    factory.sessions[1].add.assert_not_called()


async def test_start_intent_unknown_commit_cannot_be_replayed_as_permission() -> None:
    """B の応答喪失は不明として返し、二回目も True を返さない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.bind()
    factory = BudgetSessions(db, fail_on=1)
    store = PostgresRunBudgetStore(factory)  # type: ignore[arg-type]
    with pytest.raises(BudgetStartUncertainError):
        await store.start_execution(db.claimed, execution_key="primary", **db.bound_arguments())
    assert len(factory.sessions) == 1
    assert db.reservations[0].status == "START_INTENT"
    assert not await store.start_execution(
        db.claimed, execution_key="primary", **db.bound_arguments()
    )


async def test_start_permission_is_not_returned_before_commit() -> None:
    """repository が返っても transaction の終了前には model を起動できない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.bind()
    factory = BudgetSessions(db)
    factory.commit_entered, factory.commit_release = asyncio.Event(), asyncio.Event()
    store = PostgresRunBudgetStore(factory)  # type: ignore[arg-type]
    task = asyncio.create_task(
        store.start_execution(db.claimed, execution_key="primary", **db.bound_arguments())
    )
    try:
        await asyncio.wait_for(factory.commit_entered.wait(), 2)
        assert not task.done()
        factory.commit_release.set()
        assert await task
    finally:
        factory.commit_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_usage_lost_response_is_deduplicated_and_conflicts_are_committed() -> None:
    """C の再送は一度だけ加算し、矛盾の停止理由を例外 rollback で消さない。"""

    db = BudgetDatabase()
    await db.reserve()
    await db.start()
    claim = await db.reconciler()
    store = PostgresRunBudgetStore(BudgetSessions(db, fail_on=1))  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await store.record_usage(claim, report("usage", 3, 30))
    assert (await store.record_usage(claim, report("usage", 3, 30))).disposition == "REPLAY"
    assert db.account.consumed_turns == 3
    result = await store.record_usage(claim, report("usage", 7, 70))
    assert result.disposition == "CONFLICT"
    assert db.account.block_code == "receipt_conflict"
    assert len(db.receipts) == 2
