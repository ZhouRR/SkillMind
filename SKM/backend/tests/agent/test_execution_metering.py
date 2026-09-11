"""実 Engine と偽 SDK で原 Result の観測境界を確認する。計量・課金・停止の証明ではない。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import (
    AssistantMessage,
    DeferredToolUse,
    Message,
    RateLimitEvent,
    RateLimitInfo,
)

from skillmind.agent.claude import (
    CLAUDE_AGENT_SDK_VERSION,
    CLAUDE_CODE_CLI_VERSION,
    ClaudeRuntimeConfiguration,
)
from skillmind.agent.claude_build import bundled_claude_build
from skillmind.agent.claude_metering import capture_invocation
from skillmind.agent.domain import (
    AgentEvent,
    AgentEventType,
    AgentSessionRef,
    ForkContext,
    ResumeContext,
)
from skillmind.agent.engine import ClaudeAgentSdkEngine
from skillmind.agent.metering import (
    AgentInvocationMode,
    ResultUsageObservation,
    UsageValueKind,
)
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.interaction import INTERACTION_REQUEST_SDK_NAME
from tests.agent.test_claude_engine import (
    ClientFactory,
    _result,
    _run_context,
    _session_id,
    _successful_messages,
)

Observer = Callable[[ResultUsageObservation], Awaitable[None]]


def _engine(factory: ClientFactory, observer: Observer | None) -> ClaudeAgentSdkEngine:
    """ネットワークを持たない client と、明示的な受信先だけを engine へ注入する。"""

    return ClaudeAgentSdkEngine(
        mcp_server_factory=lambda _context: {
            "type": "sdk",
            "name": "skillmind",
            "instance": object(),
        },
        configuration=ClaudeRuntimeConfiguration(environment={}),
        client_factory=factory,
        usage_observer=observer,
        interrupt_drain_timeout_seconds=2,
    )


class Observations:
    """永続化や BudgetUsageReport への変換を行わない、await 順序の記録先。"""

    def __init__(self) -> None:
        """試験ごとに独立した原観測列を用意する。"""

        self.items: list[ResultUsageObservation] = []

    async def __call__(self, observation: ResultUsageObservation) -> None:
        """Engine が引き渡した型をそのまま保持し、零や final を補わない。"""

        self.items.append(observation)


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    """全 event を局部 task で消費し、失敗と終了を呼出し側へ返す。"""

    return [event async for event in stream]


@pytest.mark.parametrize("mode", ["initial", "resume", "fork"])
async def test_observation_binds_actual_final_options_and_prompt(tmp_path: Path, mode: str) -> None:
    """Context の概略でなく、resume/fork の変更後に client へ渡した option を固定する。"""

    context = _run_context(tmp_path)
    context = replace(context, limits=replace(context.limits, max_turns=7, max_budget_usd=0.125))
    parent = AgentSessionRef(context.run_id, uuid4(), str(uuid4()))
    factory = ClientFactory(_successful_messages)
    observed = Observations()
    engine = _engine(factory, observed)
    if mode == "initial":
        stream = engine.execute(context)
    elif mode == "resume":
        stream = engine.resume(ResumeContext(context, parent, input_text="Resume exact input"))
    else:
        stream = engine.fork(ForkContext(context, parent, input_text="Fork exact input"))
    events = await _collect(stream)
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED
    assert len(observed.items) == 1
    item = observed.items[0]
    invocation = item.invocation
    client = factory.clients[0]
    options = client.options
    assert isinstance(invocation.invocation_id, UUID)
    assert invocation.project_id == context.project_id
    assert invocation.run_id == context.run_id
    assert invocation.run_attempt_id == context.run_attempt_id
    assert invocation.user_id == context.user_id
    assert invocation.session_id == _session_id(options)
    assert invocation.mode is AgentInvocationMode(mode.upper())
    assert invocation.parent_session_id == (None if mode == "initial" else parent.session_id)
    assert client.prompt is not None
    assert invocation.prompt_checksum == sha256_hex(client.prompt)
    assert invocation.sdk_version == CLAUDE_AGENT_SDK_VERSION
    assert invocation.cli_version == CLAUDE_CODE_CLI_VERSION
    assert invocation.options.model == options.model == context.model
    assert invocation.options.max_turns == options.max_turns == 7
    assert invocation.options.max_budget_usd.kind is UsageValueKind.BINARY64
    assert invocation.options.max_budget_usd.value == (0.125).hex()
    assert invocation.options.session_id == options.session_id
    assert invocation.options.resume == options.resume
    assert invocation.options.fork_session is options.fork_session
    assert invocation.options.continue_conversation is options.continue_conversation
    assert invocation.options.output_format_checksum == (
        sha256_hex(canonical_json(options.output_format))
    )
    assert item.observation_key == f"claude-result/{invocation.invocation_id}"
    assert invocation.options.checksum == sha256_hex(canonical_json(invocation.options.to_json()))
    assert client.disconnected


async def test_resume_same_session_and_attempt_still_has_distinct_invocation(
    tmp_path: Path,
) -> None:
    """Session/Attempt の再利用を一回の課金実行と誤認せず、各呼出しを区別する。"""

    context = _run_context(tmp_path)
    parent = AgentSessionRef(context.run_id, uuid4(), str(uuid4()))
    factory = ClientFactory(_successful_messages)
    observed = Observations()
    engine = _engine(factory, observed)
    for _ in range(2):
        await _collect(engine.resume(ResumeContext(context, parent, input_text="Same input")))
    first, second = observed.items
    assert first.invocation.session_id == second.invocation.session_id == parent.session_id
    assert first.invocation.run_attempt_id == second.invocation.run_attempt_id
    assert first.invocation.invocation_id != second.invocation.invocation_id
    assert first.observation_key != second.observation_key
    assert first.invocation.prompt_checksum == second.invocation.prompt_checksum


async def test_duplicate_results_keep_original_observation_key(tmp_path: Path) -> None:
    """同じ呼出しの異なる Result を、新しい payload hash 鍵で別計量へ逃がさない。"""

    def messages(options: ClaudeAgentOptions) -> list[Message]:
        """同一呼出しで矛盾する原値を返し、下流で衝突を検出できる鍵を要求する。"""

        first = _result(_session_id(options))
        second = replace(first, num_turns=2)
        return [first, second]

    factory = ClientFactory(messages)
    observed = Observations()
    await _collect(_engine(factory, observed).execute(_run_context(tmp_path)))
    first, second = observed.items
    assert first.observation_key == second.observation_key
    assert first.invocation is second.invocation
    assert first.turns.value == 1 and second.turns.value == 2


@pytest.mark.parametrize("terminal", ["success", "error", "defer"])
async def test_result_observation_precedes_every_mapper_yield_and_stream_close(
    tmp_path: Path, terminal: str
) -> None:
    """Result 内の診断 usage が最初に yield されても、原値はそれ以前に observer へ渡る。"""

    def messages(options: ClaudeAgentOptions) -> list[Message]:
        """Result の成功/失敗/待機を変え、観測対象の選別に使わせない。"""

        message = _result(_session_id(options), is_error=terminal == "error")
        if terminal == "defer":
            message.deferred_tool_use = DeferredToolUse(
                id="deferred",
                name=INTERACTION_REQUEST_SDK_NAME,
                input={},
            )
        return [message]

    factory = ClientFactory(messages)
    observed = Observations()
    stream = _engine(factory, observed).execute(_run_context(tmp_path))
    try:
        first = await anext(stream)
        assert first.event_type is AgentEventType.USAGE_UPDATED
        assert len(observed.items) == 1
        assert observed.items[0].turns.kind is UsageValueKind.INTEGER
        assert observed.items[0].turns.value == 1
    finally:
        await stream.aclose()
    assert factory.clients[0].disconnected
    assert len(observed.items) == 1


async def test_observer_is_awaited_before_success_is_visible(tmp_path: Path) -> None:
    """非同期 sink が完了する前に成功/待機 event を消費者へ公開しない。"""

    entered, release = asyncio.Event(), asyncio.Event()

    async def observe(_observation: ResultUsageObservation) -> None:
        """外部 I/O の代わりに局部 Event で保存待機を再現する。"""

        entered.set()
        await release.wait()

    factory = ClientFactory(lambda options: [_result(_session_id(options))])
    task = asyncio.create_task(_collect(_engine(factory, observe).execute(_run_context(tmp_path))))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not task.done()
        release.set()
        events = await asyncio.wait_for(task, timeout=2)
        assert events[-1].event_type is AgentEventType.RESULT_COMPLETED
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert factory.clients[0].disconnected


async def test_interrupted_result_is_observed_without_claiming_stop_or_final(
    tmp_path: Path,
) -> None:
    """ユーザー割込みの Result も原観測とし、停止証明や完全計量へ自動昇格させない。"""

    context = _run_context(tmp_path)
    factory = ClientFactory(_successful_messages, wait_for_interrupt=True)
    observed = Observations()
    engine = _engine(factory, observed)
    task = asyncio.create_task(_collect(engine.execute(context)))
    try:
        while not factory.clients:
            await asyncio.sleep(0)
        client = factory.clients[0]
        await asyncio.wait_for(client.receive_started.wait(), timeout=2)
        await engine.interrupt(
            AgentSessionRef(context.run_id, context.run_attempt_id, _session_id(client.options))
        )
        events = await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert events[-1].event_type is AgentEventType.SESSION_INTERRUPTED
    assert len(observed.items) == 1 and client.disconnected
    item = observed.items[0]
    assert not hasattr(item, "final") and not hasattr(item, "stop_confirmed")
    assert not hasattr(item, "cost_nanos") and not hasattr(item, "watermark")


async def test_usage_diagnostics_and_missing_result_do_not_manufacture_observations(
    tmp_path: Path,
) -> None:
    """Rate limit と Assistant usage は加算せず、Result 欠落もゼロ用量に変換しない。"""

    def messages(options: ClaudeAgentOptions) -> list[Message]:
        """診断用の二つの同名 usage 系列だけを返す。"""

        session_id = _session_id(options)
        return [
            AssistantMessage(
                content=[], model="test", session_id=session_id, usage={"input_tokens": 99}
            ),
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(status="rejected", utilization=0.9),
                uuid=str(uuid4()),
                session_id=session_id,
            ),
        ]

    factory = ClientFactory(messages)
    observed = Observations()
    events = await _collect(_engine(factory, observed).execute(_run_context(tmp_path)))
    assert [event.event_type for event in events] == [
        AgentEventType.USAGE_UPDATED,
        AgentEventType.USAGE_UPDATED,
        AgentEventType.ENGINE_FAILED,
    ]
    assert events[-1].payload["reason"] == "result_message_missing"
    assert observed.items == [] and factory.clients[0].disconnected


@pytest.mark.parametrize("terminal", ["success", "error", "defer"])
async def test_observer_failure_blocks_result_and_is_secret_safe(
    tmp_path: Path, terminal: str
) -> None:
    """sink の失敗を隠して成功/待機せず、例外本文も AgentEvent に混入させない。"""

    def messages(options: ClaudeAgentOptions) -> list[Message]:
        """観測後に公開してはいけない三種類の Result を用意する。"""

        message = _result(_session_id(options), is_error=terminal == "error")
        if terminal == "defer":
            message.deferred_tool_use = DeferredToolUse("defer", INTERACTION_REQUEST_SDK_NAME, {})
        return [message]

    async def reject(_observation: ResultUsageObservation) -> None:
        """模擬例外本文を公開せず、分類だけを返すことを検査する。"""

        raise RuntimeError("synthetic-private-observer-detail")

    factory = ClientFactory(messages)
    events = await _collect(_engine(factory, reject).execute(_run_context(tmp_path)))
    assert len(events) == 1 and events[0].event_type is AgentEventType.ENGINE_FAILED
    assert events[0].payload["reason"] == "metering_observation_failed"
    assert "synthetic-private-observer-detail" not in repr(events)
    assert factory.clients[0].disconnected


async def test_cancellation_during_observation_propagates_and_disconnects(tmp_path: Path) -> None:
    """観測 await の取消しを通常失敗や成功へ変えず、所有 task が client を閉じる。"""

    entered = asyncio.Event()

    async def pending(_observation: ResultUsageObservation) -> None:
        """task 取消しまで待ち、背景書込み task は作らない。"""

        entered.set()
        await asyncio.Event().wait()

    factory = ClientFactory(lambda options: [_result(_session_id(options))])
    task = asyncio.create_task(_collect(_engine(factory, pending).execute(_run_context(tmp_path))))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert factory.clients[0].disconnected


async def test_foreign_session_result_never_reaches_observer(tmp_path: Path) -> None:
    """SDK Result の session identity を観測より前に確認する。"""

    factory = ClientFactory(lambda _options: [_result(str(uuid4()))])
    observed = Observations()
    events = await _collect(_engine(factory, observed).execute(_run_context(tmp_path)))
    assert observed.items == []
    assert len(events) == 1 and events[0].event_type is AgentEventType.ENGINE_FAILED
    assert factory.clients[0].disconnected


async def test_observer_cleanup_cannot_swallow_pending_cancellation(tmp_path: Path) -> None:
    """sink が取消しを後処理して戻っても、取消し済み呼出しは結果を yield しない。"""

    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def cleanup_only(_observation: ResultUsageObservation) -> None:
        """uncancel せずに後処理だけ行う依存先を再現する。"""

        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaned.set()

    factory = ClientFactory(lambda options: [_result(_session_id(options))])
    task = asyncio.create_task(
        _collect(_engine(factory, cleanup_only).execute(_run_context(tmp_path)))
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set() and factory.clients[0].disconnected


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        (None, UsageValueKind.MISSING, None),
        (0, UsageValueKind.INTEGER, 0),
        (3, UsageValueKind.INTEGER, 3),
        (0.1, UsageValueKind.BINARY64, (0.1).hex()),
        (True, UsageValueKind.INVALID, None),
        (False, UsageValueKind.INVALID, None),
        (-1, UsageValueKind.INVALID, None),
        (-0.1, UsageValueKind.INVALID, None),
        (float("nan"), UsageValueKind.INVALID, None),
        (float("inf"), UsageValueKind.INVALID, None),
        ("3", UsageValueKind.INVALID, None),
        ({"value": 3}, UsageValueKind.INVALID, None),
    ],
)
async def test_raw_result_numbers_keep_type_and_never_coerce_into_exact_usage(
    tmp_path: Path, raw: Any, kind: UsageValueKind, value: int | str | None
) -> None:
    """SDK dataclass の注解を信用せず、浮動小数を十進固定精度やゼロにすり替えない。"""

    def messages(options: ClaudeAgentOptions) -> list[Message]:
        """parser が原値を無検証で通し得る境界を同じ Result 型で再現する。"""

        message = _result(_session_id(options))
        message.num_turns = raw
        message.total_cost_usd = raw
        return [message]

    factory = ClientFactory(messages)
    observed = Observations()
    await _collect(_engine(factory, observed).execute(_run_context(tmp_path)))
    item = observed.items[0]
    expected_turn_kind = UsageValueKind.INVALID if kind is UsageValueKind.BINARY64 else kind
    expected_turn_value = None if kind is UsageValueKind.BINARY64 else value
    assert item.turns.kind is expected_turn_kind and item.turns.value == expected_turn_value
    assert item.cost_usd.kind is kind and item.cost_usd.value == value


async def test_observation_is_frozen_and_contains_no_prompt_results_or_arbitrary_usage(
    tmp_path: Path,
) -> None:
    """後から SDK の可変辞書を触っても、凍結観測へ本文や Secret が流れ込まない。"""

    context = replace(_run_context(tmp_path), prompt="synthetic-private-prompt")
    message_holder = []

    def messages(options: ClaudeAgentOptions) -> list[Message]:
        """結果本文・任意 usage・上流 error は観測の allowlist から除外する。"""

        message = _result(_session_id(options))
        message.result = "synthetic-private-result"
        message.usage = {"untrusted": "synthetic-private-usage"}
        message.errors = ["synthetic-private-error"]
        message_holder.append(message)
        return [message]

    factory = ClientFactory(messages)
    observed = Observations()
    await _collect(_engine(factory, observed).execute(context))
    item = observed.items[0]
    before = asdict(item)
    message_holder[0].num_turns = 50
    factory.clients[0].options.max_turns = 100
    assert asdict(item) == before
    encoded = json.dumps(before, default=str)
    assert "synthetic-private" not in encoded
    for target, field in (
        (item, "turns"),
        (item.invocation, "mode"),
        (item.invocation.options, "max_turns"),
        (item.turns, "value"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, field, None)


async def test_observer_disabled_preserves_existing_result_mapping(tmp_path: Path) -> None:
    """未接線の運用に計量済みの主張や新しい SDK 起動要件を先行導入しない。"""

    factory = ClientFactory(_successful_messages)
    events = await _collect(_engine(factory, None).execute(_run_context(tmp_path)))
    assert [event.event_type for event in events] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.USAGE_UPDATED,
        AgentEventType.RESULT_COMPLETED,
    ]
    assert factory.clients[0].disconnected


@pytest.mark.parametrize(
    "changes",
    [
        {"max_turns": True},
        {"max_turns": 0},
        {"max_turns": -1},
        {"max_turns": None},
        {"max_turns": 1.0},
        {"max_turns": 2**63},
        {"continue_conversation": True},
        {"continue_conversation": 0},
        {"fork_session": 1},
    ],
)
async def test_invalid_actual_options_fail_before_client_factory_or_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    """暗黙の履歴継続や無効な上限を、接続後に正当な計量対象として扱わない。"""

    factory = ClientFactory(_successful_messages)
    observed = Observations()
    engine = _engine(factory, observed)
    original = engine._base_options
    monkeypatch.setattr(
        engine, "_base_options", lambda context: replace(original(context), **changes)
    )
    with pytest.raises(ValueError, match="invocation options"):
        await _collect(engine.execute(_run_context(tmp_path)))
    assert factory.clients == [] and observed.items == []


@pytest.mark.parametrize(
    "mode,option_mode",
    [
        (AgentInvocationMode.INITIAL, "resume"),
        (AgentInvocationMode.RESUME, "initial"),
        (AgentInvocationMode.FORK, "initial"),
        (AgentInvocationMode.FORK, "self-parent"),
    ],
)
def test_invocation_mode_cannot_disagree_with_actual_session_options(
    tmp_path: Path, mode: AgentInvocationMode, option_mode: str
) -> None:
    """resume/fork を新規実行とラベルし直して、歴史を除外した計量へ見せない。"""

    session_id = str(uuid4())
    options = ClaudeAgentOptions(
        max_turns=5, session_id=session_id, cli_path=bundled_claude_build().cli_path
    )
    if option_mode == "resume":
        options.session_id = None
        options.resume = session_id
    elif option_mode == "self-parent":
        options.resume = session_id
        options.fork_session = True
    with pytest.raises(ValueError, match="invocation mode"):
        capture_invocation(
            _run_context(tmp_path),
            session_id=session_id,
            prompt="Exact prompt",
            options=options,
            mode=mode,
        )
