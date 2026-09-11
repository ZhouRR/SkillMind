"""Claude Agent SDK session lifecycle と Skillmind event 変換を実装する。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    HookEventMessage,
    Message,
    MirrorErrorMessage,
    RateLimitEvent,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    SessionStore,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from skillmind.agent.claude import (
    CLAUDE_AGENT_SDK_VERSION,
    CLAUDE_CODE_CLI_VERSION,
    ClaudeRuntimeConfiguration,
    ToolAuthorizationCallback,
    ToolDenialCallback,
    build_claude_agent_options,
)
from skillmind.agent.claude_build import require_bundled_cli
from skillmind.agent.claude_metering import capture_invocation, capture_result_usage
from skillmind.agent.compatibility import probe_claude_agent_sdk
from skillmind.agent.domain import (
    AgentEvent,
    AgentEventType,
    AgentSessionRef,
    EngineHealth,
    EngineHealthStatus,
    ForkContext,
    ResumeContext,
    RunContext,
)
from skillmind.agent.metering import (
    AgentInvocationMode,
    BeforeInvocationConnect,
    ExecutionUsageObserver,
)
from skillmind.core.cancellation import check_pending_cancellation
from skillmind.core.json_text import strip_code_fence
from skillmind.effects.proposal import CHANGE_PROPOSE_SDK_NAME
from skillmind.runs.budget import BudgetUnavailableError
from skillmind.runs.interaction import INTERACTION_REQUEST_SDK_NAME

_DEFAULT_RESUME_PROMPT = "Continue the existing Skillmind run from its saved session."
_DEFAULT_FORK_PROMPT = "Produce an alternative result from the saved read-only session."


class ClaudeClient(Protocol):
    """Engine が使用する ClaudeSDKClient の最小 interface。"""

    async def connect(self, prompt: str | None = None) -> None:
        """CLI subprocess を開始し、最初の prompt を送信する。"""

        ...

    def receive_response(self) -> AsyncIterator[Message]:
        """ResultMessage まで SDK message を返す。"""

        ...

    async def interrupt(self) -> None:
        """活動中 query へ interrupt を送信する。"""

        ...

    async def disconnect(self) -> None:
        """CLI subprocess と一時 materialization を解放する。"""

        ...


ClaudeClientFactory = Callable[[ClaudeAgentOptions], ClaudeClient]
McpServerFactory = Callable[[RunContext], Any]


@dataclass(frozen=True, slots=True)
class RunMcpRuntime:
    """Run-scoped MCP server と PreToolUse audit callback の組。"""

    server: Any
    on_tool_authorized: ToolAuthorizationCallback | None = None
    on_tool_denied: ToolDenialCallback | None = None
    deferred_tool_names: frozenset[str] = frozenset()


def _default_client_factory(options: ClaudeAgentOptions) -> ClaudeClient:
    """Production 用 ClaudeSDKClient を共有 protocol として生成する。"""

    require_bundled_cli(options.cli_path)
    return ClaudeSDKClient(options)


class ClaudeMessageMapper:
    """SDK message を secret を含まない AgentEvent へ決定的に変換する。"""

    def __init__(self, context: RunContext, session_id: str) -> None:
        """Run identity、SDK session ID、次の global sequence を保持する。"""

        self._context = context
        self._session_id = _validated_session_id(session_id)
        self._next_sequence = context.sequence_start

    def map(self, message: Message, *, interrupted: bool = False) -> tuple[AgentEvent, ...]:
        """一つの SDK message からゼロ個以上の正規化 event を生成する。"""

        observed_session_id = _message_session_id(message)
        if observed_session_id is not None and observed_session_id != self._session_id:
            raise ValueError("Claude SDK message session ID does not match the active session")

        mapped: list[AgentEvent] = []
        if isinstance(message, MirrorErrorMessage):
            mapped.append(
                self._event(
                    AgentEventType.SESSION_STORE_DEGRADED,
                    {"reason": "session_store_mirror_error"},
                )
            )
        elif isinstance(message, HookEventMessage):
            mapped.extend(self._map_hook_event(message))
        elif isinstance(message, TaskStartedMessage):
            mapped.append(
                self._event(
                    AgentEventType.STEP_STARTED,
                    {
                        "step_id": message.task_id,
                        "description": message.description,
                        "task_type": message.task_type,
                        "parent_tool_use_id": message.tool_use_id,
                    },
                )
            )
        elif isinstance(message, TaskProgressMessage):
            mapped.append(
                self._event(
                    AgentEventType.USAGE_UPDATED,
                    {"step_id": message.task_id, "usage": dict(message.usage)},
                )
            )
        elif isinstance(message, TaskNotificationMessage):
            event_type = (
                AgentEventType.STEP_COMPLETED
                if message.status == "completed"
                else AgentEventType.STEP_FAILED
            )
            mapped.append(
                self._event(
                    event_type,
                    {
                        "step_id": message.task_id,
                        "status": message.status,
                        "summary": message.summary,
                    },
                )
            )
        elif isinstance(message, TaskUpdatedMessage):
            if message.status in TERMINAL_TASK_STATUSES:
                event_type = (
                    AgentEventType.STEP_COMPLETED
                    if message.status == "completed"
                    else AgentEventType.STEP_FAILED
                )
                mapped.append(
                    self._event(
                        event_type,
                        {"step_id": message.task_id, "status": message.status},
                    )
                )
        elif isinstance(message, AssistantMessage):
            mapped.extend(self._map_content(message.content))
            if message.usage:
                mapped.append(
                    self._event(
                        AgentEventType.USAGE_UPDATED,
                        {"model": message.model, "usage": dict(message.usage)},
                    )
                )
            if message.error:
                mapped.append(
                    self._event(
                        AgentEventType.STEP_FAILED,
                        {"reason": "assistant_error", "category": message.error},
                    )
                )
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            mapped.extend(self._map_content(message.content))
        elif isinstance(message, StreamEvent):
            delta = extract_text_delta(message.event)
            if delta is not None:
                mapped.append(self._event(AgentEventType.TEXT_DELTA, {"text": delta}))
        elif isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            mapped.append(
                self._event(
                    AgentEventType.USAGE_UPDATED,
                    {
                        "rate_limit": {
                            "status": info.status,
                            "type": info.rate_limit_type,
                            "utilization": info.utilization,
                            "resets_at": info.resets_at,
                        }
                    },
                )
            )
        elif isinstance(message, ResultMessage):
            mapped.extend(self._map_result(message, interrupted=interrupted))
        elif isinstance(message, SystemMessage) and message.subtype in {"init", "initialize"}:
            mapped.append(
                self._event(
                    AgentEventType.SESSION_STARTED,
                    {
                        "model": message.data.get("model"),
                        "claude_code_version": message.data.get("claude_code_version"),
                        "permission_mode": message.data.get("permissionMode"),
                    },
                )
            )
        return tuple(mapped)

    def engine_failure(self, reason: str, *, error_type: str | None = None) -> AgentEvent:
        """例外 text を公開せず、分類だけを終端 event として返す。"""

        payload: dict[str, Any] = {"reason": reason}
        if error_type is not None:
            payload["error_type"] = error_type
        return self._event(AgentEventType.ENGINE_FAILED, payload)

    def _map_content(self, blocks: list[Any]) -> list[AgentEvent]:
        """思考 chain を保存せず、完成 text と Tool lifecycle だけを変換する。"""

        mapped: list[AgentEvent] = []
        for block in blocks:
            if isinstance(block, TextBlock):
                mapped.append(self._event(AgentEventType.TEXT_COMPLETED, {"text": block.text}))
            elif isinstance(block, ToolUseBlock):
                mapped.append(
                    self._event(
                        AgentEventType.TOOL_REQUESTED,
                        {
                            "tool_use_id": block.id,
                            "tool_name": block.name,
                            "input_keys": sorted(block.input),
                        },
                    )
                )
            elif isinstance(block, ToolResultBlock):
                mapped.append(
                    self._event(
                        AgentEventType.TOOL_FAILED
                        if block.is_error
                        else AgentEventType.TOOL_COMPLETED,
                        {"tool_use_id": block.tool_use_id},
                    )
                )
            elif isinstance(block, (ServerToolUseBlock, ServerToolResultBlock)):
                mapped.append(
                    self._event(
                        AgentEventType.TOOL_FAILED,
                        {"reason": "server_tool_not_allowed"},
                    )
                )
        return mapped

    def _map_hook_event(self, message: HookEventMessage) -> list[AgentEvent]:
        """Hook response の allow/deny/defer だけを監査 event として残す。"""

        if message.subtype != "hook_response":
            return []
        decision = _permission_decision(message.data)
        if decision is None:
            return []
        event_type = (
            AgentEventType.PERMISSION_REQUIRED
            if decision == "defer"
            else AgentEventType.PERMISSION_RESOLVED
        )
        return [
            self._event(
                event_type,
                {"hook_event": message.hook_event_name, "decision": decision},
            )
        ]

    def _map_result(self, message: ResultMessage, *, interrupted: bool) -> list[AgentEvent]:
        """Usage を先に出力し、Result ごとに一つだけ終端 event を生成する。"""

        mapped: list[AgentEvent] = []
        if message.usage:
            mapped.append(self._event(AgentEventType.USAGE_UPDATED, {"usage": message.usage}))
        common: dict[str, Any] = {
            "subtype": message.subtype,
            "stop_reason": message.stop_reason,
            "num_turns": message.num_turns,
            "duration_ms": message.duration_ms,
            "duration_api_ms": message.duration_api_ms,
            "total_cost_usd": message.total_cost_usd,
        }
        if interrupted:
            mapped.append(self._event(AgentEventType.SESSION_INTERRUPTED, common))
        elif message.deferred_tool_use is not None:
            common["deferred_tool"] = {
                "tool_use_id": message.deferred_tool_use.id,
                "tool_name": message.deferred_tool_use.name,
                "input_keys": sorted(message.deferred_tool_use.input),
            }
            if message.deferred_tool_use.name == INTERACTION_REQUEST_SDK_NAME:
                common["interaction_request"] = dict(message.deferred_tool_use.input)
                mapped.append(self._event(AgentEventType.INTERACTION_REQUESTED, common))
            elif message.deferred_tool_use.name == CHANGE_PROPOSE_SDK_NAME:
                common["change_proposal_request"] = dict(message.deferred_tool_use.input)
                mapped.append(self._event(AgentEventType.CHANGE_PROPOSED, common))
            else:
                mapped.append(self._event(AgentEventType.SESSION_DEFERRED, common))
        elif message.is_error:
            common.update(
                {
                    "api_error_status": message.api_error_status,
                    "error_count": len(message.errors or ()),
                    "permission_denial_count": len(message.permission_denials or ()),
                }
            )
            mapped.append(self._event(AgentEventType.ENGINE_FAILED, common))
        else:
            structured_output, source = _structured_output(message)
            common["structured_output"] = structured_output
            if source is not None:
                common["structured_output_source"] = source
            if structured_output is None:
                common["result"] = message.result
            mapped.append(self._event(AgentEventType.RESULT_COMPLETED, common))
        return mapped

    def _event(self, event_type: AgentEventType, payload: Mapping[str, Any]) -> AgentEvent:
        """UTC timestamp と Run 全体の連続 sequence を一箇所で付与する。"""

        event = AgentEvent(
            run_id=self._context.run_id,
            run_attempt_id=self._context.run_attempt_id,
            agent_session_id=self._session_id,
            sequence=self._next_sequence,
            occurred_at=datetime.now(UTC),
            event_type=event_type,
            payload=payload,
        )
        self._next_sequence += 1
        return event


def _structured_output(message: ResultMessage) -> tuple[Any, str | None]:
    """SDK structured output を優先し、raw result は厳密な JSON object だけを採用する。

    Prompt 契約に反して model が JSON を単一の code fence で包む場合があるため、
    fence の除去だけは決定的な前処理として許可する。Markdown 本文の推測変換は行わない。
    """

    if message.structured_output is not None:
        return message.structured_output, "sdk_output_format"
    if not isinstance(message.result, str):
        return None, None
    try:
        parsed = json.loads(strip_code_fence(message.result))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed, "result_json_fallback"


@dataclass(slots=True)
class _ActiveExecution:
    """Interrupt と receive loop が共有する一つの活動中 SDK session。"""

    session_ref: AgentSessionRef
    client: ClaudeClient
    done: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_requested: bool = False


class ClaudeAgentSdkEngine:
    """固定 Claude Agent SDK を AgentEngine 契約へ適合させる adapter。"""

    def __init__(
        self,
        *,
        mcp_server_factory: McpServerFactory,
        configuration: ClaudeRuntimeConfiguration,
        session_store: SessionStore | None = None,
        client_factory: ClaudeClientFactory = _default_client_factory,
        interrupt_drain_timeout_seconds: float = 30.0,
        usage_observer: ExecutionUsageObserver | None = None,
        before_connect: BeforeInvocationConnect | None = None,
    ) -> None:
        """Run-scoped MCP と受信 factory/観測先を保持する。

        Factory は渡された options を変更・無視せず適用するサービス内部 port とする。
        観測先の有無は共有予算の許可ではなく、未設定なら既存の表示 event 経路を保つ。
        """

        if (
            not math.isfinite(interrupt_drain_timeout_seconds)
            or interrupt_drain_timeout_seconds <= 0
        ):
            raise ValueError("Interrupt drain timeout must be finite and positive")
        if before_connect is not None and usage_observer is None:
            raise ValueError("A budget start gate requires its usage observer")
        self._mcp_server_factory = mcp_server_factory
        self._configuration = configuration
        self._session_store = session_store
        self._client_factory = client_factory
        self._interrupt_drain_timeout_seconds = interrupt_drain_timeout_seconds
        self._usage_observer = usage_observer
        self._before_connect = before_connect
        self._active: dict[AgentSessionRef, _ActiveExecution] = {}
        self._active_lock = asyncio.Lock()

    def has_invocation_callbacks(
        self, before_connect: BeforeInvocationConnect, observer: ExecutionUsageObserver
    ) -> bool:
        """Executor の預留だけを接いで Engine gate を忘れる装配を検出する。"""
        return self._before_connect == before_connect and self._usage_observer == observer

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """事前採番した UUID で新規 session を開始する。"""

        session_id = (
            context.prepared_invocation.session_id
            if context.prepared_invocation is not None
            else str(uuid4())
        )
        options = replace(self._base_options(context), session_id=session_id)
        async with aclosing(
            self._run(context, session_id, context.prompt, options, AgentInvocationMode.INITIAL)
        ) as stream:
            async for event in stream:
                yield event

    async def resume(self, context: ResumeContext) -> AsyncIterator[AgentEvent]:
        """同一 Run の保存済み session を新しい Attempt で再開する。"""

        _validate_parent_session(context.run, context.session)
        options = replace(self._base_options(context.run), resume=context.session.session_id)
        prompt = context.input_text or _DEFAULT_RESUME_PROMPT
        async with aclosing(
            self._run(
                context.run, context.session.session_id, prompt, options, AgentInvocationMode.RESUME
            )
        ) as stream:
            async for event in stream:
                yield event

    async def fork(self, context: ForkContext) -> AsyncIterator[AgentEvent]:
        """読み取り専用 session から事前採番した候補 session を作成する。"""

        _validate_parent_session(context.run, context.parent_session)
        session_id = (
            context.run.prepared_invocation.session_id
            if context.run.prepared_invocation is not None
            else str(uuid4())
        )
        options = replace(
            self._base_options(context.run),
            resume=context.parent_session.session_id,
            session_id=session_id,
            fork_session=True,
        )
        prompt = context.input_text or _DEFAULT_FORK_PROMPT
        async with aclosing(
            self._run(context.run, session_id, prompt, options, AgentInvocationMode.FORK)
        ) as stream:
            async for event in stream:
                yield event

    async def interrupt(self, session_ref: AgentSessionRef) -> None:
        """Interrupt を送り、receive loop が Result を drain して close するまで待つ。"""

        async with self._active_lock:
            active = self._active.get(session_ref)
            if active is None:
                raise LookupError("Agent session is not active in this engine process")
            active.interrupt_requested = True
        try:
            # Control 応答が止まる場合も同じ期限へ含める。done waiter を shield した
            # 背景 task にせず、timeout/呼出し側取消しでこの待機自体を閉じる。
            async with asyncio.timeout(self._interrupt_drain_timeout_seconds):
                try:
                    await active.client.interrupt()
                finally:
                    await check_pending_cancellation()
                await active.done.wait()
        except Exception as error:
            # 制御要求の失敗も close を試みる。Worker が監視側の例外を抑制しても、
            # 元 query を放置しない。close の返却は停止証明や未決使用量の解放ではない。
            try:
                await active.client.disconnect()
            finally:
                await check_pending_cancellation()
            if isinstance(error, TimeoutError):
                raise TimeoutError("Claude SDK interrupt drain timed out") from None
            raise

    async def health(self) -> EngineHealth:
        """Model を呼ばず、固定 SDK/CLI interface の互換性を返す。"""

        try:
            report = probe_claude_agent_sdk()
        except Exception as error:
            return EngineHealth(
                status=EngineHealthStatus.UNAVAILABLE,
                engine="claude-agent-sdk",
                sdk_version=CLAUDE_AGENT_SDK_VERSION,
                cli_version=CLAUDE_CODE_CLI_VERSION,
                details={"error_type": type(error).__name__},
            )
        return EngineHealth(
            status=EngineHealthStatus.AVAILABLE,
            engine="claude-agent-sdk",
            sdk_version=report.sdk_version,
            cli_version=report.cli_version,
            details={"option_fields": report.option_fields},
        )

    def _base_options(self, context: RunContext) -> ClaudeAgentOptions:
        """一つの Run に必要な最小 MCP server と SessionStore を組み立てる。"""

        runtime = self._mcp_server_factory(context)
        if isinstance(runtime, RunMcpRuntime):
            server = runtime.server
            on_tool_authorized = runtime.on_tool_authorized
            on_tool_denied = runtime.on_tool_denied
            deferred_tool_names = runtime.deferred_tool_names
        else:
            server = runtime
            on_tool_authorized = None
            on_tool_denied = None
            deferred_tool_names = frozenset()
        return build_claude_agent_options(
            context,
            mcp_server=server,
            configuration=self._configuration,
            session_store=self._session_store,
            on_tool_authorized=on_tool_authorized,
            on_tool_denied=on_tool_denied,
            deferred_tool_names=deferred_tool_names,
        )

    async def _run(
        self,
        context: RunContext,
        session_id: str,
        prompt: str,
        options: ClaudeAgentOptions,
        mode: AgentInvocationMode,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Connect、message drain、disconnect を一つの所有範囲で完結させる。"""

        mapper = ClaudeMessageMapper(context, session_id)
        prepared = context.prepared_invocation
        if prepared is not None and self._before_connect is None:
            raise BudgetUnavailableError("A prepared invocation requires its durable start gate")
        invocation = (
            capture_invocation(
                context,
                session_id=session_id,
                prompt=prompt,
                options=options,
                mode=mode,
                invocation_id=prepared.invocation_id if prepared is not None else None,
            )
            if self._usage_observer is not None
            else None
        )
        if prepared is not None and invocation != prepared:
            raise BudgetUnavailableError("Prepared invocation does not match the actual execution")
        if self._before_connect is not None:
            assert invocation is not None
            await check_pending_cancellation()
            try:
                permitted = await self._before_connect(invocation)
            finally:
                # 依存先の収尾が取消を捕えて戻る場合も、許可や別エラーに置換しない。
                await check_pending_cancellation()
            if permitted is not True:
                raise BudgetUnavailableError(
                    "Original start intent does not authorize a new launch"
                )
        client = self._client_factory(options)
        session_ref = AgentSessionRef(
            run_id=context.run_id,
            run_attempt_id=context.run_attempt_id,
            session_id=session_id,
        )
        active = _ActiveExecution(session_ref=session_ref, client=client)
        saw_result = False
        disconnect_error: Exception | None = None
        try:
            if self._before_connect is not None:
                assert invocation is not None
                await check_pending_cancellation()
                actual = capture_invocation(
                    context,
                    session_id=session_id,
                    prompt=prompt,
                    options=options,
                    mode=mode,
                    invocation_id=invocation.invocation_id,
                )
                if actual != invocation:
                    raise BudgetUnavailableError("Client factory changed the bound invocation")
            await self._register(active)
            await check_pending_cancellation()
            await client.connect(prompt)
            await check_pending_cancellation()
            async for message in client.receive_response():
                # SDK が取消しを捕えて message を返しても、観測/表示を続行させない。
                await check_pending_cancellation()
                if isinstance(message, ResultMessage) and invocation is not None:
                    # 消費側は最初の終端/待機 yield で stream を閉じる場合がある。独立観測を
                    # 先に await し、表示 event の重複や close に最終 Result を失わせない。
                    observation = capture_result_usage(invocation, message)
                    assert self._usage_observer is not None
                    try:
                        try:
                            await self._usage_observer(observation)
                        finally:
                            # Sink が取消しを捕えて別エラーを返す場合も、表示 yield や
                            # client cleanup より前に未配送分を受けて取消しを伝播する。
                            await check_pending_cancellation()
                    except Exception as error:
                        # 監査先の失敗/commit 不明を成功や待機へ変換しない。裸取消は伝播する。
                        yield mapper.engine_failure(
                            "metering_observation_failed", error_type=type(error).__name__
                        )
                        return
                for event in mapper.map(message, interrupted=active.interrupt_requested):
                    # 一つの Result の usage yield 中に消費側が取消す場合も次を渡さない。
                    await check_pending_cancellation()
                    yield event
                    await check_pending_cancellation()
                if isinstance(message, ResultMessage):
                    saw_result = True
            await check_pending_cancellation()
            if not saw_result:
                yield mapper.engine_failure("result_message_missing")
        except Exception as error:
            # Register/connect 等が取消しを普通の例外に置換しても、失敗 event で隠さない。
            await check_pending_cancellation()
            yield mapper.engine_failure("sdk_execution_error", error_type=type(error).__name__)
        finally:
            try:
                # 消費側の cancel→aclose は次の event checkpoint を通らない。
                # 未配送の取消しを先に受けても、内側 finally の cleanup は必ず試みる。
                await check_pending_cancellation()
            finally:
                try:
                    await client.disconnect()
                except Exception as error:
                    disconnect_error = error
                finally:
                    active.done.set()
                    try:
                        # Disconnect 中に初めて取消された場合も、未配送分で登録解除を
                        # 中断させず、完了/通常エラーへ取消しをすり替えない。
                        await check_pending_cancellation()
                    finally:
                        try:
                            await self._unregister(active)
                        finally:
                            await check_pending_cancellation()
        if disconnect_error is not None:
            yield mapper.engine_failure(
                "sdk_disconnect_error", error_type=type(disconnect_error).__name__
            )

    async def _register(self, active: _ActiveExecution) -> None:
        """同一 RunAttempt/session の重複実行を process 内でも拒否する。"""

        async with self._active_lock:
            if active.session_ref in self._active:
                raise RuntimeError("Agent session is already active")
            self._active[active.session_ref] = active

    async def _unregister(self, active: _ActiveExecution) -> None:
        """別実行で置換されていない場合だけ活動中 session を除去する。"""

        async with self._active_lock:
            if self._active.get(active.session_ref) is active:
                del self._active[active.session_ref]


def _validated_session_id(session_id: str) -> str:
    """SDK/契約共通の UUID session ID を検証する。"""

    try:
        return str(UUID(session_id))
    except ValueError as error:
        raise ValueError("Claude SDK session ID must be a UUID") from error


def _validate_parent_session(context: RunContext, session: AgentSessionRef) -> None:
    """Resume/Fork が別 Run の transcript を参照することを拒否する。"""

    if session.run_id != context.run_id:
        raise ValueError("Agent session belongs to a different Run")
    _validated_session_id(session.session_id)


def _message_session_id(message: Message) -> str | None:
    """SDK message 型ごとの session ID を一つの検証経路へ集約する。"""

    if isinstance(message, (ResultMessage, StreamEvent, RateLimitEvent)):
        return message.session_id
    if isinstance(message, AssistantMessage):
        return message.session_id
    if isinstance(
        message,
        (TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage, TaskUpdatedMessage),
    ):
        return message.session_id
    if isinstance(message, HookEventMessage):
        return message.session_id
    if isinstance(message, SystemMessage):
        value = message.data.get("session_id")
        return value if isinstance(value, str) else None
    return None


def extract_text_delta(event: Mapping[str, Any]) -> str | None:
    """Raw stream event から表示用 text delta だけを抽出する。

    Engine の TEXT_DELTA と interpreter completion の streaming が同じ解釈を共有する。
    """

    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, Mapping) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) else None


def _permission_decision(data: Mapping[str, Any]) -> str | None:
    """Hook payload の既知 container から allow/deny/defer だけを抽出する。"""

    candidates: list[Mapping[str, Any]] = [data]
    for key in ("hook_response", "output", "hookSpecificOutput"):
        value = data.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
            nested = value.get("hookSpecificOutput")
            if isinstance(nested, Mapping):
                candidates.append(nested)
    for candidate in candidates:
        decision = candidate.get("permissionDecision")
        if decision in {"allow", "deny", "defer"}:
            return str(decision)
    return None
