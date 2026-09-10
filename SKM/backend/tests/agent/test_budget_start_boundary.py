"""実 Engine の起動前門禁を偽 SDK で確認する。永続予算や実 process の証明ではない。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from claude_agent_sdk import ClaudeAgentOptions, SystemMessage

from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.claude_metering import capture_invocation
from skillmind.agent.domain import (
    AgentEvent,
    AgentEventType,
    AgentSessionRef,
    ForkContext,
    ResumeContext,
    RunContext,
)
from skillmind.agent.engine import ClaudeAgentSdkEngine
from skillmind.agent.metering import AgentInvocation, AgentInvocationMode, ResultUsageObservation
from skillmind.core.hashing import sha256_hex
from skillmind.runs.budget import BudgetStartUncertainError, BudgetUnavailableError
from tests.agent.test_claude_engine import (
    ScriptedClaudeClient,
    _result,
    _run_context,
    _session_id,
)

StartCallback = Callable[[AgentInvocation], Awaitable[bool]]
UsageObserver = Callable[[ResultUsageObservation], Awaitable[None]]


@pytest.fixture(autouse=True)
def isolated_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK option 作成時も実 process 設定を読まず、局部 fake だけを使う。"""

    monkeypatch.setattr("skillmind.agent.claude.sanitized_agent_environment", lambda _config: {})


class RecordingClient(ScriptedClaudeClient):
    """既存 SDK fake に connect/disconnect の正確な呼出し回数を追加する。"""

    def __init__(self, options: ClaudeAgentOptions, timeline: list[str]) -> None:
        """Result は元 Session の実 options から作り、別の認証源を用意しない。"""

        super().__init__(options, [_result(_session_id(options))])
        self.timeline = timeline
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self, prompt: str | None = None) -> None:
        """許可より先に呼ばれた connect も隠さず記録する。"""

        self.timeline.append("connect")
        self.connect_calls += 1
        await super().connect(prompt)

    async def disconnect(self) -> None:
        """未接続 client も所有者が一度だけ解放したことを記録する。"""

        self.timeline.append("disconnect")
        self.disconnect_calls += 1
        await super().disconnect()


class RecordingFactory:
    """構造以外の副作用を持たない factory と、その契約違反を故意に作る fake。"""

    def __init__(
        self,
        timeline: list[str],
        *,
        after_create: Callable[[ClaudeAgentOptions], None] | None = None,
    ) -> None:
        """同期取消や option 変更を実接続せず注入する。"""

        self.timeline = timeline
        self.after_create = after_create
        self.clients: list[RecordingClient] = []
        self.created = asyncio.Event()

    def __call__(self, options: ClaudeAgentOptions) -> RecordingClient:
        """元 options の client を先に記録し、後続の失敗でも清理を観察可能にする。"""

        self.timeline.append("factory")
        client = RecordingClient(options, self.timeline)
        self.clients.append(client)
        self.created.set()
        if self.after_create is not None:
            self.after_create(options)
        return client


class RecordingCallbacks:
    """起動許可と原 Result の順序だけを記録し、DB 提交を偽装しない。"""

    def __init__(self, timeline: list[str]) -> None:
        """各 test で原 invocation と観測列を分離する。"""

        self.timeline = timeline
        self.invocations: list[AgentInvocation] = []
        self.observations: list[ResultUsageObservation] = []

    async def before_connect(self, invocation: AgentInvocation) -> bool:
        """唯一の許可 callback が受け取った原 invocation を保持する。"""

        self.timeline.append("permit")
        self.invocations.append(invocation)
        return True

    async def observe(self, observation: ResultUsageObservation) -> None:
        """Result を受けても停止、final、予算結算の事実を作らない。"""

        self.timeline.append("observe")
        self.observations.append(observation)


def _engine(
    factory: RecordingFactory,
    observer: UsageObserver | None,
    before_connect: StartCallback | None = None,
) -> ClaudeAgentSdkEngine:
    """起動 guard は明示的に渡し、既存の観測専用 path へ自動導入しない。"""

    return ClaudeAgentSdkEngine(
        mcp_server_factory=lambda _context: {
            "type": "sdk",
            "name": "skillmind",
            "instance": object(),
        },
        configuration=ClaudeRuntimeConfiguration(environment={}),
        client_factory=factory,
        usage_observer=observer,
        before_connect=before_connect,
    )


async def _collect(stream: AsyncIterator[AgentEvent], events: list[AgentEvent]) -> None:
    """例外前に漏れた event も外側の list に残す。"""

    async for event in stream:
        events.append(event)


async def _cancel_dependency_owner(handling: str) -> None:
    """依存先での未配送・捕捉・別例外への置換を、実 task の cancel で再現する。"""

    owner = asyncio.current_task()
    assert owner is not None
    owner.cancel()
    if handling == "pending":
        return
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        if handling == "replace_error":
            raise RuntimeError("Synthetic dependency replaced cancellation") from None


def _operation(
    engine: ClaudeAgentSdkEngine,
    context: RunContext,
    mode: str,
    parent: AgentSessionRef | None = None,
) -> AsyncIterator[AgentEvent]:
    """主三経路を同一 Engine 境界へ通し、model や別業務実装を使わない。"""

    if parent is None:
        parent = AgentSessionRef(context.run_id, uuid4(), str(uuid4()))
    if mode == "initial":
        return engine.execute(context)
    if mode == "resume":
        return engine.resume(ResumeContext(context, parent, input_text="Original resume input"))
    return engine.fork(ForkContext(context, parent, input_text="Original fork input"))


@pytest.mark.parametrize("mode", ["initial", "resume", "fork"])
async def test_permission_precedes_factory_and_result_uses_same_invocation(
    tmp_path: Path, mode: str
) -> None:
    """開始前と Result が同一 identity を共有し、一つの許可で一回だけ接続する。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    engine = _engine(factory, callbacks.observe, callbacks.before_connect)
    events: list[AgentEvent] = []
    await _collect(_operation(engine, _run_context(tmp_path), mode), events)

    assert timeline == ["permit", "factory", "connect", "observe", "disconnect"]
    assert len(callbacks.invocations) == len(callbacks.observations) == 1
    invocation = callbacks.invocations[0]
    assert callbacks.observations[0].invocation is invocation
    assert callbacks.observations[0].observation_key == (
        f"claude-result/{invocation.invocation_id}"
    )
    client = factory.clients[0]
    assert client.connect_calls == client.disconnect_calls == 1
    assert invocation.mode.value == mode.upper()
    assert invocation.session_id == _session_id(client.options)
    assert invocation.options.max_turns == client.options.max_turns
    assert invocation.options.model == client.options.model
    assert client.prompt is not None and invocation.prompt_checksum == sha256_hex(client.prompt)
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED


async def test_pending_permission_does_not_construct_client_or_emit_session(tmp_path: Path) -> None:
    """DB の代わりに Event で許可待ちを固定し、待機中の SDK 副作用を禁止する。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    entered, release = asyncio.Event(), asyncio.Event()

    async def permit(invocation: AgentInvocation) -> bool:
        """許可の明示戻り値まで factory に進ませない。"""

        await callbacks.before_connect(invocation)
        entered.set()
        await release.wait()
        return True

    events: list[AgentEvent] = []
    task = asyncio.create_task(
        _collect(
            _engine(factory, callbacks.observe, permit).execute(_run_context(tmp_path)), events
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert timeline == ["permit"] and not factory.clients and not events
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert timeline == ["permit", "factory", "connect", "observe", "disconnect"]


@pytest.mark.parametrize("answer", [False, None, 1, "yes", {}])
async def test_only_exact_true_grants_start_without_synthetic_session(
    tmp_path: Path, answer: Any
) -> None:
    """既存意図・未返答・truthy 値を新規開始の明示許可として扱わない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    async def permit(invocation: AgentInvocation) -> bool:
        """故意に注釈と異なる値を返し、runtime の厳格判定を要求する。"""

        await callbacks.before_connect(invocation)
        return cast(bool, answer)

    events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(
            _engine(factory, callbacks.observe, permit).execute(_run_context(tmp_path)), events
        )
    assert timeline == ["permit"]
    assert not factory.clients and not callbacks.observations and not events


@pytest.mark.parametrize("failure", ["unavailable", "uncertain", "unexpected"])
async def test_permission_failure_propagates_original_without_retry_or_factory(
    tmp_path: Path, failure: str
) -> None:
    """保存失敗や B の提交不明を、SDK failure event や再試行許可へ変換しない。"""

    errors = {
        "unavailable": BudgetUnavailableError("Not authorized"),
        "uncertain": BudgetStartUncertainError("Original start remains unknown"),
        "unexpected": RuntimeError("Synthetic callback failure"),
    }
    expected = errors[failure]
    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    async def permit(invocation: AgentInvocation) -> bool:
        """一度の原失敗を返し、何度呼ばれても別許可へ変えない。"""

        await callbacks.before_connect(invocation)
        raise expected

    events: list[AgentEvent] = []
    with pytest.raises(type(expected)) as caught:
        await _collect(
            _engine(factory, callbacks.observe, permit).execute(_run_context(tmp_path)), events
        )
    assert caught.value is expected
    assert timeline == ["permit"] and not factory.clients and not events
    assert not callbacks.observations


def test_start_guard_requires_usage_observer_at_construction() -> None:
    """起動だけ許可して原 Result の観測先を欠く組合せを装配時に拒否する。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    with pytest.raises(ValueError):
        _engine(factory, None, callbacks.before_connect)
    assert not timeline and not factory.clients


@pytest.mark.parametrize("with_observer", [False, True])
async def test_legacy_path_does_not_enable_start_guard(tmp_path: Path, with_observer: bool) -> None:
    """未注入・観測専用の両方で既存 path を保ち、予算導入済みとみなさない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    events: list[AgentEvent] = []
    engine = _engine(factory, callbacks.observe if with_observer else None)
    await _collect(engine.execute(_run_context(tmp_path)), events)
    assert timeline == ["factory", "connect", *(["observe"] if with_observer else []), "disconnect"]
    assert not callbacks.invocations
    assert len(callbacks.observations) == int(with_observer)
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED


@pytest.mark.parametrize("handling", ["propagate", "swallow", "replace_error"])
async def test_permission_cancellation_cannot_become_start_authority(
    tmp_path: Path, handling: str
) -> None:
    """許可 callback の清理が取消を吞んだり別例外へ変えても、所有 task の取消を優先する。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    entered = asyncio.Event()

    async def permit(invocation: AgentInvocation) -> bool:
        """局部待機だけを取消し、実 DB や背景処理を起動しない。"""

        await callbacks.before_connect(invocation)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if handling == "propagate":
                raise
            if handling == "replace_error":
                raise RuntimeError("Cleanup replaced cancellation") from None
        return True

    events: list[AgentEvent] = []
    task = asyncio.create_task(
        _collect(
            _engine(factory, callbacks.observe, permit).execute(_run_context(tmp_path)), events
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert timeline == ["permit"] and not factory.clients and not events
    assert not callbacks.observations


@pytest.mark.parametrize("when", ["before_callback", "callback_return", "factory_return"])
async def test_pending_cancellation_is_checked_at_synchronous_start_boundaries(
    tmp_path: Path, when: str
) -> None:
    """まだ例外になっていない cancel 要求も、次の callback/factory/connect を止める。"""

    timeline: list[str] = []
    callbacks = RecordingCallbacks(timeline)

    def cancel_owner() -> None:
        """次の await まで待たずに所有 task へ取消意図を記録する。"""

        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel()

    def after_create(_options: ClaudeAgentOptions) -> None:
        """構造の途中に取消された client を、接続せず清理させる。"""

        if when == "factory_return":
            cancel_owner()

    async def permit(invocation: AgentInvocation) -> bool:
        """B の明示許可と cancel が同じ tick に成立する状況を作る。"""

        await callbacks.before_connect(invocation)
        if when == "callback_return":
            cancel_owner()
        return True

    factory = RecordingFactory(timeline, after_create=after_create)
    events: list[AgentEvent] = []

    async def execute() -> None:
        """pytest 自身でなく独立 task を同期取消の所有者にする。"""

        if when == "before_callback":
            cancel_owner()
        await _collect(
            _engine(factory, callbacks.observe, permit).execute(_run_context(tmp_path)), events
        )

    task = asyncio.create_task(execute())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    expected = {
        "before_callback": [],
        "callback_return": ["permit"],
        "factory_return": ["permit", "factory", "disconnect"],
    }
    assert timeline == expected[when]
    assert not events and not callbacks.observations
    assert len(factory.clients) == int(when == "factory_return")
    if factory.clients:
        assert factory.clients[0].connect_calls == 0
        assert factory.clients[0].disconnect_calls == 1


@pytest.mark.parametrize(
    "field", ["model", "max_turns", "max_budget_usd", "session_id", "resume", "output_format"]
)
async def test_factory_cannot_change_previously_authorized_options(
    tmp_path: Path, field: str
) -> None:
    """許可後の option drift は接続せず閉じ、原 invocation の許可を流用しない。"""

    timeline: list[str] = []
    callbacks = RecordingCallbacks(timeline)

    def mutate(options: ClaudeAgentOptions) -> None:
        """mutable SDK options の浅い値と入れ子 Schema の双方を故意に変更する。"""

        if field == "output_format":
            assert options.output_format is not None
            options.output_format["schema"]["title"] = "Different output contract"
        else:
            values = {
                "model": "different-model",
                "max_turns": 99,
                "max_budget_usd": 0.25,
                "session_id": str(uuid4()),
                "resume": str(uuid4()),
            }
            setattr(options, field, values[field])

    factory = RecordingFactory(timeline, after_create=mutate)
    events: list[AgentEvent] = []
    with suppress(ValueError, BudgetUnavailableError):
        await _collect(
            _engine(factory, callbacks.observe, callbacks.before_connect).execute(
                _run_context(tmp_path)
            ),
            events,
        )
    assert timeline == ["permit", "factory", "disconnect"]
    assert len(callbacks.invocations) == len(factory.clients) == 1
    assert not callbacks.observations
    assert all(event.event_type is AgentEventType.ENGINE_FAILED for event in events)
    assert factory.clients[0].connect_calls == 0
    assert factory.clients[0].disconnect_calls == 1


@pytest.mark.parametrize("cancel_after_registration", [False, True])
async def test_registration_boundary_preserves_cancellation_and_client_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_after_registration: bool
) -> None:
    """許可後の登録待機または登録直後に取消されても、接続せず owned client を閉じる。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    engine = _engine(factory, callbacks.observe, callbacks.before_connect)
    original_register = engine._register
    if cancel_after_registration:

        async def register(active: Any) -> None:
            """実登録の後で cancel を保留し、connect 直前の再検証を要求する。"""

            await original_register(active)
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel()

        monkeypatch.setattr(engine, "_register", register)
    else:
        await engine._active_lock.acquire()
    events: list[AgentEvent] = []
    task = asyncio.create_task(_collect(engine.execute(_run_context(tmp_path)), events))
    try:
        await asyncio.wait_for(factory.created.wait(), timeout=2)
        if not cancel_after_registration:
            assert not task.done()
            task.cancel()
            engine._active_lock.release()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if engine._active_lock.locked():
            engine._active_lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert timeline == ["permit", "factory", "disconnect"]
    assert not events and not callbacks.observations and not engine._active
    assert factory.clients[0].connect_calls == 0
    assert factory.clients[0].disconnect_calls == 1


async def test_repeated_resume_requires_another_explicit_permission(tmp_path: Path) -> None:
    """同じ Session の二度目の呼出しへ、最初の開始許可を保存して再利用しない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    async def permit(invocation: AgentInvocation) -> bool:
        """二度目は既存 START_INTENT と同じ False を返すだけで、保存を模倣しない。"""

        await callbacks.before_connect(invocation)
        return len(callbacks.invocations) == 1

    context = _run_context(tmp_path)
    parent = AgentSessionRef(context.run_id, uuid4(), str(uuid4()))
    engine = _engine(factory, callbacks.observe, permit)
    first_events: list[AgentEvent] = []
    await _collect(engine.resume(ResumeContext(context, parent, "Same input")), first_events)
    second_events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(engine.resume(ResumeContext(context, parent, "Same input")), second_events)
    first, second = callbacks.invocations
    assert first.session_id == second.session_id == parent.session_id
    assert first.invocation_id != second.invocation_id
    assert first.prompt_checksum == second.prompt_checksum
    assert timeline == ["permit", "factory", "connect", "observe", "disconnect", "permit"]
    assert len(factory.clients) == len(callbacks.observations) == 1
    assert not second_events


@pytest.mark.parametrize(
    ("boundary", "handling"),
    [
        ("factory", "pending"),
        ("register", "pending"),
        ("register", "swallow"),
        ("register", "replace_error"),
        ("observer", "pending"),
        ("observer", "swallow"),
        ("observer", "replace_error"),
    ],
)
async def test_cancellation_checkpoint_precedes_suspending_client_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, handling: str
) -> None:
    """取消しを先に配送し、二回以上 await する client cleanup を最後まで所有する。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    entered, release = asyncio.Event(), asyncio.Event()

    def configure(_options: ClaudeAgentOptions) -> None:
        """終了回数だけでは見えない、実際に中断可能な cleanup を SDK fake に設定する。"""

        client = factory.clients[-1]
        original_disconnect = client.disconnect

        async def disconnect() -> None:
            """cancel の未配送例外で finally 内の await が再中断されないか観察する。"""

            await original_disconnect()
            timeline.append("cleanup-start")
            entered.set()
            await asyncio.sleep(0)
            await release.wait()
            await asyncio.sleep(0)
            timeline.append("cleanup-end")

        monkeypatch.setattr(client, "disconnect", disconnect)
        if boundary == "factory":
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel()

    async def observe(observation: ResultUsageObservation) -> None:
        """実 Result の callback 内で取消し、Engine が event 化する前の gate を要求する。"""

        await callbacks.observe(observation)
        if boundary == "observer":
            await _cancel_dependency_owner(handling)

    factory.after_create = configure
    engine = _engine(factory, observe, callbacks.before_connect)
    original_register = engine._register
    if boundary == "register":

        async def register(active: Any) -> None:
            """登録自身は実装を使い、依存先の収尾による取消し変換だけを注入する。"""

            await original_register(active)
            await _cancel_dependency_owner(handling)

        monkeypatch.setattr(engine, "_register", register)
    events: list[AgentEvent] = []
    task = asyncio.create_task(_collect(engine.execute(_run_context(tmp_path)), events))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not events and not engine._active
    assert timeline[-2:] == ["cleanup-start", "cleanup-end"]
    assert len(factory.clients) == 1 and factory.clients[0].disconnect_calls == 1
    assert factory.clients[0].connect_calls == int(boundary == "observer")


def _prepared_context(
    engine: ClaudeAgentSdkEngine, context: RunContext, mode: str
) -> tuple[RunContext, AgentSessionRef]:
    """原 descriptor の読戻しを純構造で再現し、実保存や B 成功とは宣言しない。"""

    parent = AgentSessionRef(context.run_id, uuid4(), str(uuid4()))
    session_id = parent.session_id if mode == "resume" else str(uuid4())
    options = engine._base_options(context)
    if mode == "initial":
        options = replace(options, session_id=session_id)
        prompt = context.prompt
    elif mode == "resume":
        options = replace(options, resume=parent.session_id)
        prompt = "Original resume input"
    else:
        options = replace(
            options, session_id=session_id, resume=parent.session_id, fork_session=True
        )
        prompt = "Original fork input"
    invocation = capture_invocation(
        context,
        session_id=session_id,
        prompt=prompt,
        options=options,
        mode=AgentInvocationMode(mode.upper()),
    )
    return replace(context, prepared_invocation=invocation), parent


@pytest.mark.parametrize("mode", ["initial", "resume", "fork"])
async def test_prepared_descriptor_preserves_original_invocation_and_sdk_session(
    tmp_path: Path, mode: str
) -> None:
    """原 descriptor が完全一致した場合だけ、その ID のまま改めて B の許可を求める。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    engine = _engine(factory, callbacks.observe, callbacks.before_connect)
    context, parent = _prepared_context(engine, _run_context(tmp_path), mode)
    prepared = context.prepared_invocation
    assert prepared is not None
    events: list[AgentEvent] = []
    await _collect(_operation(engine, context, mode, parent), events)
    assert callbacks.invocations == [prepared]
    assert callbacks.observations[0].invocation == prepared
    assert callbacks.observations[0].observation_key == (f"claude-result/{prepared.invocation_id}")
    assert _session_id(factory.clients[0].options) == prepared.session_id
    assert timeline == ["permit", "factory", "connect", "observe", "disconnect"]
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED


@pytest.mark.parametrize(
    "field",
    ["project_id", "run_id", "run_attempt_id", "user_id", "prompt", "model", "limits", "schema"],
)
async def test_prepared_descriptor_mismatch_never_rebinds_or_requests_permission(
    tmp_path: Path, field: str
) -> None:
    """原対象・指令・上界・Schema が変わっても、新しい ID を採って同じ預留に結ばない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    engine = _engine(factory, callbacks.observe, callbacks.before_connect)
    context, parent = _prepared_context(engine, _run_context(tmp_path), "initial")
    prepared = context.prepared_invocation
    changes: dict[str, Any]
    if field in {"project_id", "run_id", "run_attempt_id", "user_id"}:
        changes = {field: uuid4()}
    elif field == "limits":
        changes = {"limits": replace(context.limits, max_turns=context.limits.max_turns + 1)}
    elif field == "schema":
        changes = {"result_schema": {"type": "object", "title": "Different schema"}}
    else:
        changes = {field: "Different frozen input"}
    changed = replace(context, **changes)
    events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(_operation(engine, changed, "initial", parent), events)
    assert changed.prepared_invocation is prepared
    assert not timeline and not events and not factory.clients
    assert not callbacks.invocations and not callbacks.observations


@pytest.mark.parametrize("mode", ["initial", "resume", "fork"])
async def test_prepared_descriptor_does_not_authorize_another_operation(
    tmp_path: Path, mode: str
) -> None:
    """同じ保存済み descriptor を execute/resume/fork 間で付け替えて起動させない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    engine = _engine(factory, callbacks.observe, callbacks.before_connect)
    context, parent = _prepared_context(engine, _run_context(tmp_path), mode)
    other_mode = {"initial": "resume", "resume": "fork", "fork": "initial"}[mode]
    events: list[AgentEvent] = []
    # 不整合な fork identity は pure validator が先に拒否してよい。副作用の不在を守る。
    with pytest.raises((ValueError, BudgetUnavailableError)):
        await _collect(_operation(engine, context, other_mode, parent), events)
    assert not timeline and not events and not factory.clients


@pytest.mark.parametrize("with_observer", [False, True])
async def test_prepared_descriptor_without_start_guard_is_not_a_legacy_fallback(
    tmp_path: Path, with_observer: bool
) -> None:
    """束縛を持つ実行だけは gate 未注入を拒否し、旧無予算 path に落とさない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    engine = _engine(factory, callbacks.observe if with_observer else None)
    context, parent = _prepared_context(engine, _run_context(tmp_path), "initial")
    events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(_operation(engine, context, "initial", parent), events)
    assert not timeline and not events and not factory.clients and not callbacks.observations


async def test_same_prepared_descriptor_requires_fresh_explicit_start_decision(
    tmp_path: Path,
) -> None:
    """保存済み原 descriptor 自体を再使用可能な開始券とみなさない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    async def permit(invocation: AgentInvocation) -> bool:
        """最初だけ True、同じ束縛の二度目は既存意図を示す False を返す。"""

        await callbacks.before_connect(invocation)
        return len(callbacks.invocations) == 1

    engine = _engine(factory, callbacks.observe, permit)
    context, parent = _prepared_context(engine, _run_context(tmp_path), "resume")
    events: list[AgentEvent] = []
    await _collect(_operation(engine, context, "resume", parent), events)
    rejected_events: list[AgentEvent] = []
    with pytest.raises(BudgetUnavailableError):
        await _collect(_operation(engine, context, "resume", parent), rejected_events)
    assert callbacks.invocations == [context.prepared_invocation, context.prepared_invocation]
    assert len(factory.clients) == len(callbacks.observations) == 1
    assert not rejected_events
    assert timeline == ["permit", "factory", "connect", "observe", "disconnect", "permit"]


@pytest.mark.parametrize("boundary", ["connect", "receive_message", "receive_end"])
@pytest.mark.parametrize("handling", ["pending", "swallow", "replace_error"])
@pytest.mark.parametrize("with_guard", [False, True], ids=["observer-only", "budget-guard"])
async def test_cancelled_sdk_dependency_cannot_advance_or_emit_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    handling: str,
    with_guard: bool,
) -> None:
    """SDK の取消し捕捉も次の receive・普通 event・Result 欠落エラーへ変換しない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    def configure(_options: ClaudeAgentOptions) -> None:
        """SDK I/O の応答だけを変え、実 model や外部 task は作らない。"""

        client = factory.clients[-1]
        original_connect, original_disconnect = client.connect, client.disconnect

        async def connect(prompt: str | None = None) -> None:
            """接続完了の返答が取消しを隠しても、新しい receive を始めさせない。"""

            await original_connect(prompt)
            if boundary == "connect":
                await _cancel_dependency_owner(handling)

        async def receive() -> AsyncIterator[Any]:
            """取消された await から通常 message または stream 終了が返る状況を再現する。"""

            timeline.append("receive")
            if boundary != "connect":
                await _cancel_dependency_owner(handling)
            if boundary != "receive_end":
                yield SystemMessage(
                    subtype="init", data={"session_id": _session_id(client.options)}
                )
                yield _result(_session_id(client.options))

        async def disconnect() -> None:
            """清理の終了まで観察し、遅れて配送される cancel による中断を検出する。"""

            await original_disconnect()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            timeline.append("cleanup-end")

        monkeypatch.setattr(client, "connect", connect)
        monkeypatch.setattr(client, "receive_response", receive)
        monkeypatch.setattr(client, "disconnect", disconnect)

    factory.after_create = configure
    engine = _engine(factory, callbacks.observe, callbacks.before_connect if with_guard else None)
    events: list[AgentEvent] = []
    task = asyncio.create_task(_collect(engine.execute(_run_context(tmp_path)), events))
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not events and not callbacks.observations and not engine._active
    assert timeline == [
        *(["permit"] if with_guard else []),
        "factory",
        "connect",
        *([] if boundary == "connect" else ["receive"]),
        "disconnect",
        "cleanup-end",
    ]
    assert factory.clients[0].connect_calls == factory.clients[0].disconnect_calls == 1


@pytest.mark.parametrize("with_guard", [False, True], ids=["observer-only", "budget-guard"])
async def test_cancel_after_usage_yield_cannot_emit_same_result_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_guard: bool
) -> None:
    """同じ SDK Result の event 間で消費者が取消したら、次の anext は成功を返さない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    def configure(_options: ClaudeAgentOptions) -> None:
        """stream を閉じるだけでなく、所有 client の非同期収尾完了も確認する。"""

        client = factory.clients[-1]
        original_disconnect = client.disconnect

        async def disconnect() -> None:
            """event yield に残った取消しを cleanup の await へ送り込ませない。"""

            await original_disconnect()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            timeline.append("cleanup-end")

        monkeypatch.setattr(client, "disconnect", disconnect)

    factory.after_create = configure
    engine = _engine(factory, callbacks.observe, callbacks.before_connect if with_guard else None)
    events: list[AgentEvent] = []

    async def consume() -> None:
        """cancel と次の anext の間に別の await を置かず、mapper の連続 yield を調べる。"""

        stream = cast(AsyncGenerator[AgentEvent, None], engine.execute(_run_context(tmp_path)))
        try:
            first = await anext(stream)
            assert first.event_type is AgentEventType.USAGE_UPDATED
            events.append(first)
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel()
            events.append(await anext(stream))
        finally:
            await stream.aclose()

    task = asyncio.create_task(consume())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [event.event_type for event in events] == [AgentEventType.USAGE_UPDATED]
    assert len(callbacks.observations) == 1 and not engine._active
    assert timeline[-2:] == ["disconnect", "cleanup-end"]
    assert factory.clients[0].connect_calls == factory.clients[0].disconnect_calls == 1


@pytest.mark.parametrize("continuation", ["next_message", "close"])
@pytest.mark.parametrize("with_guard", [False, True], ids=["observer-only", "budget-guard"])
async def test_cancel_after_final_event_stops_receive_and_completes_direct_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation: str,
    with_guard: bool,
) -> None:
    """message 最後の yield から再開/直接 close する両方で、次の SDK 待機前に取消す。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)

    def configure(_options: ClaudeAgentOptions) -> None:
        """最初の SDK message は一 event だけ返し、次の受信開始を個別に記録する。"""

        client = factory.clients[-1]
        original_disconnect = client.disconnect

        async def receive() -> AsyncIterator[Any]:
            """次の message を表示しなくても、その受信 await を進めた事実を検出する。"""

            yield SystemMessage(subtype="init", data={"session_id": _session_id(client.options)})
            timeline.append("must-not-receive-next")
            yield _result(_session_id(client.options))

        async def disconnect() -> None:
            """aclose が直接 finally に入っても、未配送 cancel で後処理を中断させない。"""

            await original_disconnect()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            timeline.append("cleanup-end")

        monkeypatch.setattr(client, "receive_response", receive)
        monkeypatch.setattr(client, "disconnect", disconnect)

    factory.after_create = configure
    engine = _engine(factory, callbacks.observe, callbacks.before_connect if with_guard else None)
    events: list[AgentEvent] = []

    async def consume() -> None:
        """取消しを配送する余分な await を消費者側に挿入しない。"""

        stream = cast(AsyncGenerator[AgentEvent, None], engine.execute(_run_context(tmp_path)))
        try:
            events.append(await anext(stream))
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel()
            if continuation == "next_message":
                events.append(await anext(stream))
            else:
                await stream.aclose()
        finally:
            await stream.aclose()

    task = asyncio.create_task(consume())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [event.event_type for event in events] == [AgentEventType.SESSION_STARTED]
    assert not callbacks.observations and not engine._active
    assert "must-not-receive-next" not in timeline
    assert timeline[-2:] == ["disconnect", "cleanup-end"]
    assert factory.clients[0].connect_calls == factory.clients[0].disconnect_calls == 1


@pytest.mark.parametrize("completion", ["drain", "close"])
@pytest.mark.parametrize("handling", ["pending", "swallow", "replace_error"])
async def test_first_cancellation_inside_disconnect_preserves_unregister_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion: str, handling: str
) -> None:
    """正常な終了処理で初めて発生した取消しも、成功戻り値や切断エラーへ変換しない。"""

    timeline: list[str] = []
    factory, callbacks = RecordingFactory(timeline), RecordingCallbacks(timeline)
    unregistered: list[Any] = []

    def configure(_options: ClaudeAgentOptions) -> None:
        """実際の終了処理を持つ SDK fake に、最初の cancel だけを注入する。"""

        client = factory.clients[-1]
        original_disconnect = client.disconnect

        async def disconnect() -> None:
            """cleanup に入る前には取消されていないことを明示する。"""

            owner = asyncio.current_task()
            assert owner is not None and owner.cancelling() == 0
            await original_disconnect()
            await _cancel_dependency_owner(handling)

        monkeypatch.setattr(client, "disconnect", disconnect)

    factory.after_create = configure
    engine = _engine(factory, callbacks.observe, callbacks.before_connect)
    original_unregister = engine._unregister

    async def unregister(active: Any) -> None:
        """登録解除自身を await 可能にし、未配送 cancel がここを中断する窓を検出する。"""

        assert active.done.is_set()
        timeline.append("unregister-start")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await original_unregister(active)
        unregistered.append(active)
        timeline.append("unregister-end")

    monkeypatch.setattr(engine, "_unregister", unregister)
    events: list[AgentEvent] = []

    async def consume() -> None:
        """通常の完読と通常の aclose を使い、消費者からは取消さない。"""

        stream = cast(AsyncGenerator[AgentEvent, None], engine.execute(_run_context(tmp_path)))
        try:
            if completion == "drain":
                await _collect(stream, events)
            else:
                events.append(await anext(stream))
                await stream.aclose()
        finally:
            await stream.aclose()

    task = asyncio.create_task(consume())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    expected = [AgentEventType.USAGE_UPDATED]
    if completion == "drain":
        # 取消しより前に観察済みの成功は遡って消さず、cleanup 後の別 event だけを禁止する。
        expected.append(AgentEventType.RESULT_COMPLETED)
    assert [event.event_type for event in events] == expected
    assert len(unregistered) == 1 and unregistered[0].done.is_set() and not engine._active
    assert timeline[-2:] == ["unregister-start", "unregister-end"]
    assert factory.clients[0].connect_calls == factory.clients[0].disconnect_calls == 1
