"""予算 repository を実 ORM 行で動かす、SQL/transaction の明示 fake。実 DB ではない。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import RunBudgetReceipt, RunBudgetReservation
from projectmind.runs.budget import (
    BudgetPolicy,
    BudgetReconciliationClaim,
    BudgetReservationRequest,
    MeteringMode,
)
from projectmind.runs.repository_budgets import RunBudgetRepository, new_budget_account
from tests.runs.test_execution_gates import execution_rows


class BudgetDatabase:
    """SQL の対象と bind を確認して行を返し、lock 順と書込前拒否を観測する。"""

    def __init__(self, *, cost: bool = True, mode: MeteringMode = MeteringMode.CUMULATIVE) -> None:
        """Run/Attempt は稼働中、予算だけは新 Run 工場から明示的に作る。"""

        self.claimed, self.run, self.segment, self.attempt = execution_rows()
        self.run.status = "QUEUED"
        self.run.started_at = None
        self.run.limits_snapshot_json = {"max_turns": 20}
        if cost:
            self.run.limits_snapshot_json["max_budget_usd"] = "0.000000200"
        self.policy = BudgetPolicy(20, 200 if cost else None, "fixture-adapter", "fixture/v1", mode)
        self.account = new_budget_account(self.run, self.policy)
        self.run.status = "RUNNING"
        self.attempt.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        self.reservations: list[RunBudgetReservation] = []
        self.receipts: list[RunBudgetReceipt] = []
        self.lock_order: list[str] = []
        self.expire_during_budget_lock = False
        self.session = MagicMock(spec=AsyncSession)
        self.session.scalars = AsyncMock(side_effect=self.scalars)
        self.session.scalar = AsyncMock(return_value=None)
        self.session.add.side_effect = self.add
        self.repository = RunBudgetRepository(self.session)

    def add(self, row: object) -> None:
        """追加した予算行だけを受理し、RunEvent/Outbox などへの混入を検出する。"""

        if isinstance(row, RunBudgetReservation):
            self.reservations.append(row)
        elif isinstance(row, RunBudgetReceipt):
            self.receipts.append(row)
        else:
            raise AssertionError(f"Unexpected budget write: {type(row).__name__}")

    async def scalars(self, statement: Any) -> MagicMock:
        """receipt のキー条件も使い、毎回同じ行を返すだけの偽成功を避ける。"""

        table = statement.get_final_froms()[0].name
        params = statement.compile().params
        if table != "run_budget_receipts":
            assert "FOR UPDATE" in str(statement)
            self.lock_order.append(table)
        if table == "run_budget_reservations":
            if self.expire_during_budget_lock:
                self.attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            values = sorted(self.reservations, key=lambda row: row.execution_key)
        elif table == "run_budget_receipts":
            values = [
                row
                for row in self.receipts
                if row.reservation_id == params["reservation_id_1"]
                and row.receipt_key == params["receipt_key_1"]
            ]
        else:
            row = {
                "runs": self.run,
                "run_segments": self.segment,
                "run_attempts": self.attempt,
                "run_budget_accounts": self.account,
            }[table]
            values = [] if row is None else [row]
        result = MagicMock()
        result.all.return_value = values
        result.one_or_none.return_value = values[0] if values else None
        return result

    async def reserve(self, key: str = "primary", *, turns: int = 8, cost: int = 80) -> None:
        """一つの主実行だけを預留する。自動では起動しない。"""

        await self.repository.reserve_group(
            self.claimed,
            group_key=key,
            requests=(
                BudgetReservationRequest(
                    key, turns, cost if self.policy.max_cost_nanos is not None else None
                ),
            ),
        )

    async def start(self, key: str = "primary") -> None:
        """初回の意図だけが起動許可を返すことを確認する。"""

        assert await self.repository.start_execution(self.claimed, execution_key=key)

    async def reconciler(
        self, key: str = "primary", *, worker: str = "reconciler"
    ) -> BudgetReconciliationClaim:
        """元 Attempt の token と異なる核対権を取得する。"""

        return await self.repository.claim_reconciliation(
            project_id=self.run.project_id,
            run_id=self.run.id,
            execution_key=key,
            worker_id=worker,
            token=str(uuid4()),
        )
