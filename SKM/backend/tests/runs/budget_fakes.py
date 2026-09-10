"""予算 repository を実 ORM 行で動かす、SQL/transaction の明示 fake。実 DB ではない。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.metering import (
    AgentInvocation,
    AgentInvocationMode,
    InvocationOptions,
    UsageValue,
)
from skillmind.db.models import RunBudgetObservation, RunBudgetReceipt, RunBudgetReservation
from skillmind.runs.budget import (
    BudgetInvocationBinding,
    BudgetPolicy,
    BudgetReconciliationClaim,
    BudgetReservationRequest,
    MeteringMode,
)
from skillmind.runs.domain import ClaimedRun
from skillmind.runs.repository_budgets import RunBudgetRepository, new_budget_account
from tests.runs.test_execution_gates import execution_rows

# 実 credential ではない固定値。試験の同一所有者を明示し、保存行から復元しない。
START_OWNER_TOKEN = "A" * 43


def budget_invocation(claimed: ClaimedRun, *, turns: int = 8) -> AgentInvocation:
    """実測 profile と誤認できない明示 fixture の実行記述子を作る。"""

    session_id = str(uuid4())
    return AgentInvocation(
        invocation_id=uuid4(),
        project_id=claimed.project_id,
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        user_id=claimed.actor_id,
        session_id=session_id,
        mode=AgentInvocationMode.INITIAL,
        parent_session_id=None,
        prompt_checksum="1" * 64,
        options=InvocationOptions(
            model="fixture-model",
            max_turns=turns,
            max_budget_usd=UsageValue.capture(None),
            session_id=session_id,
            resume=None,
            fork_session=False,
            continue_conversation=False,
            output_format_checksum="2" * 64,
        ),
        sdk_version="fixture-sdk",
        cli_version="fixture-cli",
    )


def start_arguments(binding: BudgetInvocationBinding) -> dict[str, Any]:
    """照合 DTO を明示的な B 引数にする。確認済みの起動権は返さない。"""

    return {
        "expected_invocation_id": binding.invocation_id,
        "expected_invocation_checksum": binding.invocation_checksum,
        "start_owner_token": START_OWNER_TOKEN,
    }


class BudgetDatabase:
    """SQL の対象と bind を確認して行を返し、lock 順と書込前拒否を観測する。"""

    def __init__(self, *, cost: bool = True, mode: MeteringMode = MeteringMode.CUMULATIVE) -> None:
        """Run/Attempt は稼働中、予算だけは新 Run 工場から明示的に作る。"""

        self.claimed, self.run, self.segment, self.attempt = execution_rows()
        self.run.permission_snapshot_json = {"actor_id": str(self.claimed.actor_id)}
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
        self.observations: list[RunBudgetObservation] = []
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
        elif isinstance(row, RunBudgetObservation):
            self.observations.append(row)
        else:
            raise AssertionError(f"Unexpected budget write: {type(row).__name__}")

    async def scalars(self, statement: Any) -> MagicMock:
        """receipt のキー条件も使い、毎回同じ行を返すだけの偽成功を避ける。"""

        table = statement.get_final_froms()[0].name
        params = statement.compile().params
        values: list[Any]
        if table not in {"run_budget_receipts", "run_budget_observations"}:
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
        elif table == "run_budget_observations":
            values = [
                row
                for row in self.observations
                if row.reservation_id == params["reservation_id_1"]
                and row.observation_key == params["observation_key_1"]
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

    async def bind(self, key: str = "primary") -> BudgetInvocationBinding:
        """既存の原記述子を再送し、ない場合だけ fixture の記述子を用意する。"""

        row = next(item for item in self.reservations if item.execution_key == key)
        invocation = (
            AgentInvocation.from_json(row.invocation_json)
            if row.invocation_json is not None
            else budget_invocation(self.claimed, turns=int(row.granted_turns))
        )
        return await self.repository.bind_invocation(
            self.claimed,
            execution_key=key,
            invocation=invocation,
            start_owner_token=START_OWNER_TOKEN,
        )

    def bound_arguments(self, key: str = "primary") -> dict[str, Any]:
        """保存済みの値だけを返し、未設定のテストを自動修復しない。"""

        row = next(item for item in self.reservations if item.execution_key == key)
        assert row.invocation_id is not None and row.invocation_checksum is not None
        return start_arguments(
            BudgetInvocationBinding(row.id, row.invocation_id, row.invocation_checksum)
        )

    async def start(self, key: str = "primary") -> None:
        """初回の意図だけが起動許可を返すことを確認する。"""

        await self.bind(key)
        assert await self.repository.start_execution(
            self.claimed, execution_key=key, **self.bound_arguments(key)
        )

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
