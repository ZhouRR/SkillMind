"""実 Recorder/Store/Engine を transaction fake で接続する。実 DB・課金の証明ではない。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from projectmind.agent.domain import AgentEvent, AgentEventType
from projectmind.agent.metering import AgentInvocation, ResultUsageObservation, UsageValue
from projectmind.runs.budget import (
    BudgetConflictError,
    BudgetReservationRequest,
    BudgetStartUncertainError,
    BudgetUnavailableError,
)
from projectmind.runs.budget_execution import BudgetInvocationRecorder
from projectmind.runs.budget_store import PostgresRunBudgetStore
from tests.agent.test_budget_start_boundary import (
    RecordingFactory,
    _cancel_dependency_owner,
    _collect,
    _engine,
    _prepared_context,
)
from tests.agent.test_budget_start_boundary import (
    isolated_sdk_environment as isolated_sdk_environment,
)
from tests.agent.test_claude_engine import _run_context, _session_id
from tests.runs.budget_fakes import BudgetDatabase
from tests.runs.test_budget_store import BudgetSessions


class LedgerExecution:
    """既存の SQL/commit fake と実 callback を、一つの予約の試験依存へまとめる。"""

    def __init__(self, tmp_path: Path) -> None:
        """計量 profile は明示 fixture とし、SDK source 検証済みという主張を避ける。"""

        self.database = BudgetDatabase(cost=False)
        self.sessions = BudgetSessions(self.database)
        self.store = PostgresRunBudgetStore(self.sessions)  # type: ignore[arg-type]
        self.timeline: list[str] = []
        self.factory = RecordingFactory(self.timeline, after_create=self.check_start)
        self.recorder = self.new_recorder()
        self.engine = _engine(
            self.factory, self.recorder.observe_result, self.recorder.before_connect
        )
        context = _run_context(tmp_path)
        claimed = self.database.claimed
        self.context = replace(
            context,
            project_id=claimed.project_id,
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            user_id=claimed.actor_id,
            limits=replace(context.limits, max_turns=8),
        )
        self.events: list[AgentEvent] = []

    def new_recorder(self) -> BudgetInvocationRecorder:
        """原予約の独立 callback 所有者を作る。保存済み B は再許可しない。"""

        return BudgetInvocationRecorder(
            self.store,
            self.database.claimed,
            execution_key="primary",
            reconciliation_worker_id="fixture-reconciler",
            reconciliation_token="fixture-reconciliation-token",
        )

    async def reserve(self) -> None:
        """A も実 Store を通し、例外時に無料の SDK execution を作らない。"""

        await self.store.reserve_group(
            self.database.claimed,
            group_key="original-primary-operation",
            requests=(BudgetReservationRequest("primary", 8, None),),
        )

    def check_start(self, options: ClaudeAgentOptions) -> None:
        """factory が呼ばれた時点で原束縛と B が成立していることを要求する。"""

        row = self.database.reservations[0]
        invocation = AgentInvocation.from_json(row.invocation_json)
        assert row.status == "START_INTENT"
        assert row.start_intent_at is not None
        assert row.invocation_id == invocation.invocation_id
        assert row.invocation_checksum == invocation.checksum
        assert _session_id(options) == invocation.session_id
        assert options.max_turns == invocation.options.max_turns == row.granted_turns

    async def execute(self) -> None:
        """consumer へ漏れた event も保持し、拒否と成功を見分ける。"""

        await _collect(self.engine.execute(self.context), self.events)

    def observation(self, *, turns: int = 1) -> ResultUsageObservation:
        """保存済み原 descriptor だけで再送用観測を作り、別 invocation を発行しない。"""

        return ResultUsageObservation(
            AgentInvocation.from_json(self.database.reservations[0].invocation_json),
            UsageValue.capture(turns),
            UsageValue.capture(0.01, allow_binary64=True),
        )

    def assert_held(self) -> None:
        """観測の成否に関係なく、予約枠を消費・停止・返還へ自動変換していないか確認する。"""

        account, row = self.database.account, self.database.reservations[0]
        assert account.reserved_turns == row.reserved_turns == row.granted_turns == 8
        assert account.consumed_turns == row.consumed_turns == 0
        assert account.reserved_cost_nanos is None and account.consumed_cost_nanos is None
        assert row.reserved_cost_nanos is None and row.consumed_cost_nanos is None
        assert row.stop_confirmed_at is None and row.final_usage_at is None


async def test_recorder_binds_starts_and_records_raw_result_without_settlement(
    tmp_path: Path,
) -> None:
    """A/束縛/B/照合 claim/原観測を実装経由でつなぎ、原 Result の保存後だけ成功を渡す。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    await case.execute()
    assert len(case.sessions.sessions) == 5
    assert case.timeline == ["factory", "connect", "disconnect"]
    assert case.events[-1].event_type is AgentEventType.RESULT_COMPLETED
    assert len(case.database.observations) == 1
    observation = case.observation()
    row = case.database.observations[0]
    assert row.payload_json == observation.to_json()
    assert row.observation_key == observation.observation_key
    assert row.invocation_id == observation.invocation.invocation_id
    assert row.reservation_id == case.database.reservations[0].id
    assert not case.database.receipts
    case.assert_held()


@pytest.mark.parametrize("phase", ["binding", "start"])
async def test_sdk_factory_waits_for_binding_and_start_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """repository の行変更ではなく、Store transaction の明示終了を待って起動する。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    entered, release = asyncio.Event(), asyncio.Event()

    def block_commit() -> None:
        """対象 transaction の終了だけを局部 Event で止める。"""

        case.sessions.commit_entered, case.sessions.commit_release = entered, release

    if phase == "binding":
        block_commit()
    else:
        original = case.store.start_execution

        async def start(*args: Any, **kwargs: Any) -> bool:
            """本来の Store を置換せず、B が入る直前に commit 待機を注入する。"""

            block_commit()
            return await original(*args, **kwargs)

        monkeypatch.setattr(case.store, "start_execution", start)
    task = asyncio.create_task(case.execute())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert (
            not task.done() and not case.timeline and not case.factory.clients and not case.events
        )
        assert len(case.sessions.sessions) == (2 if phase == "binding" else 3)
        release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(case.factory.clients) == 1
    assert case.events[-1].event_type is AgentEventType.RESULT_COMPLETED
    case.assert_held()


@pytest.mark.parametrize("committed", [False, True])
async def test_binding_lost_response_only_confirms_original_descriptor(
    tmp_path: Path, committed: bool
) -> None:
    """束縛の応答喪失は原値の read-back のみで解決し、未保存なら B/factory を止める。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    case.sessions.fail_on, case.sessions.commit_before_failure = 2, committed
    if committed:
        await case.execute()
        assert case.events[-1].event_type is AgentEventType.RESULT_COMPLETED
        assert len(case.sessions.sessions) == 6
        assert len(case.factory.clients) == 1
        assert len(case.database.observations) == 1
    else:
        with pytest.raises(BudgetUnavailableError, match="not committed"):
            await case.execute()
        assert len(case.sessions.sessions) == 3
        assert not case.factory.clients and not case.events and not case.database.observations
        assert case.database.reservations[0].invocation_json is None
        assert case.database.reservations[0].status == "RESERVED"
    # 第三 transaction は confirm_only。未保存の原記述子を確認先で作らない。
    case.sessions.sessions[2].add.assert_not_called()
    case.assert_held()


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_start_never_retries_or_authorizes_factory(
    tmp_path: Path, committed: bool
) -> None:
    """B の commit 成否にかかわらず、その不明応答から接続許可を作らない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    case.sessions.fail_on, case.sessions.commit_before_failure = 3, committed
    with pytest.raises(BudgetStartUncertainError):
        await case.execute()
    row = case.database.reservations[0]
    assert row.invocation_json is not None
    assert row.status == ("START_INTENT" if committed else "RESERVED")
    assert len(case.sessions.sessions) == 3
    assert not case.timeline and not case.factory.clients and not case.events
    # 同じ callback 所有者を再利用しても B は再実行しない。
    with pytest.raises(BudgetUnavailableError, match="already been used"):
        await case.recorder.before_connect(AgentInvocation.from_json(row.invocation_json))
    assert len(case.sessions.sessions) == 3 and not case.database.observations
    case.assert_held()


async def test_prepared_existing_start_does_not_connect_again(tmp_path: Path) -> None:
    """新しい callback 所有者でも、原 START_INTENT の読戻しから二度目を起動しない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    await case.execute()
    prepared = AgentInvocation.from_json(case.database.reservations[0].invocation_json)
    repeated = replace(case.context, prepared_invocation=prepared)
    recorder = case.new_recorder()
    engine = _engine(case.factory, recorder.observe_result, recorder.before_connect)
    events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(engine.execute(repeated), events)
    assert len(case.sessions.sessions) == 7
    assert len(case.factory.clients) == len(case.database.observations) == 1
    assert not events
    assert case.database.reservations[0].invocation_id == prepared.invocation_id
    case.assert_held()


@pytest.mark.parametrize("failure", ["no_result", "connect_failure"])
async def test_missing_sdk_result_keeps_bound_start_and_full_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """B の後の接続失敗や Result 欠落でも、原 identity と未知占用を残す。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()

    def configure(options: ClaudeAgentOptions) -> None:
        """偽 SDK だけを失敗させ、Recorder/Store/Repository は実装のまま通す。"""

        case.check_start(options)
        client = case.factory.clients[-1]
        if failure == "no_result":
            client.messages = []
        else:

            async def fail_connect(_prompt: str | None = None) -> None:
                """接続を試みた事実だけを記録し、実 network を使わず失敗する。"""

                case.timeline.append("connect")
                client.connect_calls += 1
                raise ConnectionError("Synthetic SDK connect failure")

            monkeypatch.setattr(client, "connect", fail_connect)

    case.factory.after_create = configure
    await case.execute()
    assert len(case.sessions.sessions) == 3
    assert case.database.reservations[0].status == "START_INTENT"
    assert case.database.reservations[0].invocation_json is not None
    assert not case.database.observations and not case.database.receipts
    assert case.events and all(
        event.event_type is AgentEventType.ENGINE_FAILED for event in case.events
    )
    assert case.factory.clients[0].connect_calls == case.factory.clients[0].disconnect_calls == 1
    case.assert_held()


@pytest.mark.parametrize("committed", [False, True])
async def test_raw_observation_commit_unknown_blocks_success_without_new_execution(
    tmp_path: Path, committed: bool
) -> None:
    """観測の commit 不明を成功とせず、原値の明示再確認だけを許す。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    case.sessions.fail_on, case.sessions.commit_before_failure = 5, committed
    await case.execute()
    assert len(case.sessions.sessions) == 5
    assert len(case.database.observations) == int(committed)
    assert len(case.factory.clients) == 1
    assert [event.event_type for event in case.events] == [AgentEventType.ENGINE_FAILED]
    assert case.events[0].payload["reason"] == "metering_observation_failed"
    case.assert_held()
    # 明示的な原観測再送は新しいモデル呼出しではなく、同じ照合/保存経路だけを通す。
    await case.recorder.observe_result(case.observation())
    assert len(case.sessions.sessions) == 7 and len(case.database.observations) == 1
    assert len(case.factory.clients) == 1 and not case.database.receipts
    case.assert_held()


async def test_repeated_original_observation_is_deduplicated_without_accounting(
    tmp_path: Path,
) -> None:
    """原観測の再送回数を消費 turns/cost や final に加算しない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    await case.execute()
    original = case.observation()
    await case.recorder.observe_result(original)
    await case.recorder.observe_result(original)
    assert len(case.sessions.sessions) == 9
    assert len(case.database.observations) == 1 and not case.database.receipts
    assert case.database.observations[0].payload_json == original.to_json()
    assert len(case.factory.clients) == 1
    case.assert_held()


async def test_conflicting_persisted_observation_commits_block_before_result_is_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先に保存された同 slot の異内容を検出し、衝突監査を rollback せず成功を止める。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()

    def configure(options: ClaudeAgentOptions) -> None:
        """現在の Result 到着前に、同 invocation の別原観測が保存済みという故障を注入する。"""

        case.check_start(options)
        client = case.factory.clients[-1]
        original_connect = client.connect

        async def connect(prompt: str | None = None) -> None:
            """モデル結果を合成するだけでなく、先行観測も実 Recorder/Store で保存する。"""

            await original_connect(prompt)
            await case.recorder.observe_result(case.observation(turns=2))

        monkeypatch.setattr(client, "connect", connect)

    case.factory.after_create = configure
    await case.execute()
    assert [event.event_type for event in case.events] == [AgentEventType.ENGINE_FAILED]
    assert case.events[0].payload["reason"] == "metering_observation_failed"
    assert case.database.account.block_code == "observation_conflict"
    assert len(case.database.observations) == len(case.database.receipts) == 1
    assert case.database.observations[0].payload_json == case.observation(turns=2).to_json()
    assert case.database.receipts[0].kind == "UNVERIFIABLE"
    case.assert_held()
    # callback が衝突を例外にしても、既に確定した block/receipt は消えない。
    with pytest.raises(BudgetConflictError):
        await case.recorder.observe_result(case.observation())
    assert case.database.account.block_code == "observation_conflict"
    assert len(case.database.receipts) == 1 and len(case.factory.clients) == 1
    case.assert_held()


@pytest.mark.parametrize(
    "phase", ["bind_invocation", "start_execution", "claim_reconciliation", "record_observation"]
)
@pytest.mark.parametrize("handling", ["pending", "swallow", "replace_error"])
@pytest.mark.parametrize("through_engine", [False, True], ids=["recorder", "engine"])
async def test_dependency_cancellation_stops_next_budget_effect_and_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    handling: str,
    through_engine: bool,
) -> None:
    """各保存後の取消しを次の処理へ持ち越さず、Recorder 単体も Engine も成功を返さない。"""

    case = LedgerExecution(tmp_path)
    await case.reserve()
    methods = [
        "bind_invocation",
        "start_execution",
        "claim_reconciliation",
        "record_observation",
    ]
    observed: list[str] = []
    cleanup: list[str] = []

    def install(name: str) -> None:
        """transaction を合成 return で省略せず、実 Store の確定後だけ故障を加える。"""

        original = getattr(case.store, name)

        async def invoke(*args: Any, **kwargs: Any) -> Any:
            """後続副作用の有無を呼出し入口で記録し、取り消された確定も rollback と呼ばない。"""

            observed.append(name)
            result = await original(*args, **kwargs)
            if name == phase:
                await _cancel_dependency_owner(handling)
            return result

        monkeypatch.setattr(case.store, name, invoke)

    for name in methods:
        install(name)

    async def execute() -> None:
        """外側の cleanup にも実 await を置き、単に CancelledError が出ただけで完了としない。"""

        try:
            if through_engine:
                await case.execute()
            else:
                context, _parent = _prepared_context(case.engine, case.context, "initial")
                invocation = context.prepared_invocation
                assert invocation is not None
                await case.recorder.before_connect(invocation)
                await case.recorder.observe_result(case.observation())
            cleanup.append("must-not-succeed")
        finally:
            cleanup.append("begin")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            cleanup.append("end")

    task = asyncio.create_task(execute())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    expected = methods[: methods.index(phase) + 1]
    assert observed == expected
    assert len(case.sessions.sessions) == len(expected) + 1
    assert cleanup == ["begin", "end"] and not case.events
    assert len(case.database.observations) == int(phase == "record_observation")
    assert not case.database.receipts
    assert case.database.reservations[0].status == (
        "RESERVED" if phase == "bind_invocation" else "START_INTENT"
    )
    assert not case.engine._active
    started_sdk = through_engine and phase in {"claim_reconciliation", "record_observation"}
    assert len(case.factory.clients) == int(started_sdk)
    if started_sdk:
        client = case.factory.clients[0]
        assert client.connect_calls == client.disconnect_calls == 1
    case.assert_held()
