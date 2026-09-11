"""主 Executor/Engine/予算 Store の装配を実 SDK 型と局部 transaction fake で検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from skillmind.agent.domain import RunContext
from skillmind.agent.result_validation import ResultValidator
from skillmind.runs.budget import BudgetUnavailableError
from skillmind.runs.budget_store import PostgresRunBudgetStore
from skillmind.runs.domain import ClaimedRun, RunStatus
from skillmind.worker.executor import AgentRunExecutor
from skillmind.worker.primary_budget import PrimaryBudgetCoordinator
from tests.agent.test_budget_start_boundary import RecordingFactory, _engine
from tests.agent.test_budget_start_boundary import (
    isolated_sdk_environment as isolated_sdk_environment,
)
from tests.agent.test_result_validation import MemoryEvidenceLookup
from tests.runs.budget_fakes import BudgetDatabase
from tests.runs.test_budget_store import BudgetSessions
from tests.worker.test_agent_run_executor import ContextBuilder, _service


class BudgetContextBuilder(ContextBuilder):
    """原 Run と同じ認可上限を持つ Brief を返し、局部授与との区別を確認する。"""

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """本試験の policy に合わせる。モデル/資源 I/O を行わない。"""
        context = await super().build(claimed_run, sequence_start=sequence_start)
        return replace(
            context,
            limits=replace(context.limits, max_turns=20),
            task_brief={"limits": {"max_turns": 20}},
        )


class PrimaryExecution:
    """既存 fake と実主 Executor を一つの試験所有者にまとめる。"""

    def __init__(self, path: Path, *, fail_on: int | None = None, committed: bool = True):
        """計量 profile は fixture 専用で、SDK の信頼性を証明したものではない。"""
        self.db = BudgetDatabase(cost=False)
        self.db.claimed = replace(
            self.db.claimed, limits_snapshot_json=dict(self.db.run.limits_snapshot_json)
        )
        self.sessions = BudgetSessions(self.db, fail_on=fail_on, committed=committed)
        self.store = PostgresRunBudgetStore(self.sessions)  # type: ignore[arg-type]
        self.coordinator = PrimaryBudgetCoordinator(self.store, policy=self.db.policy)
        self.factory = RecordingFactory([])
        self.engine = _engine(
            self.factory, self.coordinator.observe_result, self.coordinator.before_connect
        )
        self.service = _service()
        self.executor = AgentRunExecutor(
            run_service=self.service,
            context_builder=BudgetContextBuilder(path),
            engine=self.engine,
            result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())),
            lease_seconds=60,
            budget_coordinator=self.coordinator,
        )

    async def execute(self) -> None:
        """Worker が通常利用する入口を通す。"""
        await self.executor.execute(self.db.claimed)


@pytest.mark.asyncio
async def test_main_executor_commits_reservation_binding_start_and_observation(tmp_path: Path):
    """通常の主実行を一度だけ開始し、Result 表示までの原観測を別帳簿に残す。"""
    run = PrimaryExecution(tmp_path)
    await run.execute()
    assert len(run.factory.clients) == 1
    assert run.factory.clients[0].connect_calls == 1
    row = run.db.reservations[0]
    assert row.status == "START_INTENT" and row.invocation_id is not None
    assert row.granted_turns == row.reserved_turns == 20
    assert len(run.db.observations) == 1
    assert run.db.account.consumed_turns == 0
    assert row.stop_confirmed_at is None and row.final_usage_at is None
    assert run.service.finalize_execution.await_args.kwargs["target"] is RunStatus.SUCCEEDED
    await run.execute()
    assert len(run.factory.clients) == 1


@pytest.mark.asyncio
async def test_main_executor_narrows_sdk_limit_without_rewriting_frozen_brief(tmp_path: Path):
    """確認済み停止だが用量未確定の旧占用を引き、主 SDK の実上限へ反映する。"""
    run = PrimaryExecution(tmp_path)
    await run.db.reserve(turns=8)
    await run.db.start()
    await run.db.repository.confirm_stopped(
        await run.db.reconciler(), receipt_key="stop", verified_evidence="fixture:stopped"
    )
    await run.execute()
    assert len(run.factory.clients) == 1
    assert run.factory.clients[0].options.max_turns == 12
    assert (
        run.service.freeze_agent_task_brief.await_args.kwargs["brief"]["limits"]["max_turns"] == 20
    )
    assert run.db.run.limits_snapshot_json["max_turns"] == 20
    assert run.db.account.reserved_turns == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_main_unknown_start_never_reconnects(tmp_path: Path, committed: bool):
    """B の応答喪失後、別 recorder の再試行も原起動所有権を受け継がない。"""
    run = PrimaryExecution(tmp_path, fail_on=3, committed=committed)
    await run.execute()
    assert not run.factory.clients
    await run.execute()
    assert not run.factory.clients and run.db.account.reserved_turns == 20


@pytest.mark.asyncio
async def test_main_old_unstopped_execution_blocks_new_model(tmp_path: Path):
    """旧実行の停止が不明なら、未占用の残額があっても次の主実行を起動しない。"""
    run = PrimaryExecution(tmp_path)
    await run.db.reserve(turns=8)
    await run.db.start()
    await run.execute()
    assert not run.factory.clients and len(run.db.reservations) == 1
    assert run.db.account.reserved_turns == 8


def test_executor_rejects_budget_with_uncontrolled_engine(tmp_path: Path):
    """預留だけ接続し Engine の callback を忘れた装配を起動時に拒否する。"""
    run = PrimaryExecution(tmp_path)
    with pytest.raises(ValueError, match="matching Engine"):
        AgentRunExecutor(
            run_service=run.service,
            context_builder=BudgetContextBuilder(tmp_path),
            engine=_engine(run.factory, None),
            result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())),
            lease_seconds=60,
            budget_coordinator=run.coordinator,
        )


@pytest.mark.asyncio
async def test_callback_without_primary_scope_is_denied(tmp_path: Path):
    """callback を共有 Engine へ装配しても、主実行の scope が無ければ起動を拒否する。"""
    run = PrimaryExecution(tmp_path)
    context = await BudgetContextBuilder(tmp_path).build(run.db.claimed, sequence_start=1)
    with pytest.raises(BudgetUnavailableError, match="scope is missing"):
        async for _ in run.engine.execute(context):
            pass
    assert not run.factory.clients


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_observation_commit_unknown_blocks_success_and_keeps_occupancy(
    tmp_path: Path, committed: bool
):
    """Result を読めても原観測の commit が不明なら成功を返さず、占用も減らさない。"""
    run = PrimaryExecution(tmp_path, fail_on=5, committed=committed)
    await run.execute()
    assert len(run.factory.clients) == 1
    assert len(run.db.observations) == int(committed)
    assert run.db.account.reserved_turns == 20 and run.db.account.consumed_turns == 0
    assert run.service.finalize_execution.await_args.kwargs["target"] is RunStatus.FAILED
    await run.execute()
    assert len(run.factory.clients) == 1


@pytest.mark.asyncio
async def test_missing_result_is_not_a_zero_charge_or_refund(tmp_path: Path):
    """通信が Result なしで閉じても、終態への遷移を停止/用量の根拠にしない。"""
    run = PrimaryExecution(tmp_path)
    run.factory.after_create = lambda _options: run.factory.clients[-1].messages.clear()
    await run.execute()
    assert len(run.factory.clients) == 1 and not run.db.observations
    assert run.db.account.reserved_turns == 20 and run.db.account.consumed_turns == 0
    row = run.db.reservations[0]
    assert row.stop_confirmed_at is None and row.final_usage_at is None
    assert run.service.finalize_execution.await_args.kwargs["target"] is RunStatus.FAILED


@pytest.mark.asyncio
async def test_old_run_without_account_cannot_fall_back_to_unbudgeted_engine(tmp_path: Path):
    """予算装配済み Worker が旧 Run にゼロ帳簿や局部全額を補造しない。"""
    run = PrimaryExecution(tmp_path)
    run.db.account = None
    await run.execute()
    assert not run.factory.clients and not run.db.reservations
    assert run.service.finalize_execution.await_args.kwargs["target"] is RunStatus.FAILED


@pytest.mark.asyncio
async def test_budget_marked_run_rejects_worker_without_coordinator(tmp_path: Path):
    """新 Run の予算 marker を、帳簿未装配 Worker の旧経路へ黙って流さない。"""
    run = PrimaryExecution(tmp_path)
    claimed = replace(
        run.db.claimed,
        limits_snapshot_json={
            **run.db.claimed.limits_snapshot_json,
            "budget_policy": run.db.policy.to_json(),
        },
    )
    executor = AgentRunExecutor(
        run_service=run.service,
        context_builder=BudgetContextBuilder(tmp_path),
        engine=_engine(run.factory, None),
        result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())),
        lease_seconds=60,
    )
    await executor.execute(claimed)
    assert not run.factory.clients
    run.service.prepare_execution.assert_not_called()
    assert run.service.finalize_execution.await_args.kwargs["target"] is RunStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["-", "_"])
async def test_random_reconciliation_token_accepts_urlsafe_leading_punctuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
):
    """URL-safe 乱数の先頭が記号でも、予算 identity の制約で偶発的に失敗しない。"""
    monkeypatch.setattr(
        "skillmind.worker.primary_budget.secrets.token_urlsafe", lambda _: prefix * 43
    )
    run = PrimaryExecution(tmp_path)
    await run.execute()
    assert len(run.factory.clients) == 1 and len(run.db.observations) == 1
