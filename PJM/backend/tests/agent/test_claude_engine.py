"""ClaudeAgentSdkEngine の message mapping と session lifecycle を検証する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import (
    AssistantMessage,
    Message,
    MirrorErrorMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from projectmind.agent.claude import _AGENT_ENVIRONMENT_KEYS, ClaudeRuntimeConfiguration
from projectmind.agent.domain import (
    AgentEventType,
    AgentSessionRef,
    ForkContext,
    ResumeContext,
    RunContext,
    RunLimits,
    RunWorkspace,
)
from projectmind.agent.engine import ClaudeAgentSdkEngine, ClaudeMessageMapper


def _run_context(tmp_path: Path, *, sequence_start: int = 1) -> RunContext:
    """Model を呼ばない engine test 用の immutable RunContext を返す。"""

    run_id = uuid4()
    root = tmp_path / str(run_id)
    return RunContext(
        run_id=run_id,
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        prompt="Analyze one ticket",
        task_snapshot={"capability": "jaf.ticket.analyze"},
        skill_snapshots=({"version": "m0/v1"},),
        resolved_sources={},
        permission_snapshot={"mode": "auto_read_only", "allowed_capabilities": []},
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
        limits=RunLimits(max_turns=10, wall_timeout_seconds=60, max_output_bytes=4096),
        result_schema={"type": "object"},
        tools=(),
        model="claude-test",
        sequence_start=sequence_start,
    )


def test_runtime_configuration_uses_allowlisted_fallback_without_overriding_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI の .env fallback は許可 key のみを補い、明示 environment を上書きしない。"""

    # 開発機や CI の親 process が allowlist 済み変数(ANTHROPIC_API_KEY や
    # CLAUDE_CODE_EFFORT_LEVEL など)を持っていると下の完全一致 assertion が壊れるため、
    # 先に全許可 key を除去して test を密封する。
    for key in _AGENT_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL", "environment-model")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "")
    configuration = ClaudeRuntimeConfiguration.from_environ(
        fallback={
            "ANTHROPIC_MODEL": "fallback-model",
            "ANTHROPIC_AUTH_TOKEN": "must-not-be-restored",
            "PROJECTMIND_DATABASE_URL": "must-not-be-copied",
        }
    )

    assert configuration.primary_model == "environment-model"
    assert configuration.environment == {"ANTHROPIC_MODEL": "environment-model"}


def _result(session_id: str, *, is_error: bool = False) -> ResultMessage:
    """SDK の終端条件を満たす最小 ResultMessage を構築する。"""

    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.01,
        usage={"input_tokens": 10, "output_tokens": 5},
        structured_output={"summary": "ok"} if not is_error else None,
        errors=["sensitive upstream detail"] if is_error else None,
    )


class ScriptedClaudeClient:
    """Network を使わず、指定した SDK message 列を返す test client。"""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        messages: list[Message],
        *,
        wait_for_interrupt: bool = False,
    ) -> None:
        """Options と message script を保持する。"""

        self.options = options
        self.messages = messages
        self.wait_for_interrupt = wait_for_interrupt
        self.prompt: str | None = None
        self.connected = False
        self.disconnected = False
        self.receive_started = asyncio.Event()
        self.interrupt_received = asyncio.Event()

    async def connect(self, prompt: str | None = None) -> None:
        """Prompt を記録して接続済みにする。"""

        self.prompt = prompt
        self.connected = True

    async def receive_response(self) -> AsyncIterator[Message]:
        """必要な場合は interrupt まで終端 Result を保留する。"""

        self.receive_started.set()
        for index, message in enumerate(self.messages):
            if self.wait_for_interrupt and index == len(self.messages) - 1:
                await self.interrupt_received.wait()
            yield message

    async def interrupt(self) -> None:
        """Receive loop が Result を返せるようにする。"""

        self.interrupt_received.set()

    async def disconnect(self) -> None:
        """Engine が drain 後に close したことを記録する。"""

        self.disconnected = True


class ClientFactory:
    """作成された test client と SDK options を検証可能にする factory。"""

    def __init__(
        self,
        message_builder: Callable[[ClaudeAgentOptions], list[Message]],
        *,
        wait_for_interrupt: bool = False,
    ) -> None:
        """Session ID に応じて message を作る callback を保持する。"""

        self._message_builder = message_builder
        self._wait_for_interrupt = wait_for_interrupt
        self.clients: list[ScriptedClaudeClient] = []

    def __call__(self, options: ClaudeAgentOptions) -> ScriptedClaudeClient:
        """一回の engine 実行用 client を生成して記録する。"""

        client = ScriptedClaudeClient(
            options,
            self._message_builder(options),
            wait_for_interrupt=self._wait_for_interrupt,
        )
        self.clients.append(client)
        return client


def _session_id(options: ClaudeAgentOptions) -> str:
    """新規/Fork/Resume options から有効 session ID を取得する。"""

    session_id = options.session_id or options.resume
    assert session_id is not None
    return session_id


def _successful_messages(options: ClaudeAgentOptions) -> list[Message]:
    """Initialize と成功 Result の標準 script を返す。"""

    session_id = _session_id(options)
    return [
        SystemMessage(subtype="init", data={"session_id": session_id, "model": "test"}),
        _result(session_id),
    ]


def _engine(factory: ClientFactory) -> ClaudeAgentSdkEngine:
    """Run-scoped MCP を使う test engine を構築する。"""

    return ClaudeAgentSdkEngine(
        mcp_server_factory=lambda _context: {
            "type": "sdk",
            "name": "projectmind",
            "instance": object(),
        },
        configuration=ClaudeRuntimeConfiguration(environment={}),
        client_factory=factory,
        interrupt_drain_timeout_seconds=2,
    )


def test_message_mapper_redacts_tool_input_and_preserves_global_sequence(tmp_path: Path) -> None:
    """Tool 値と thinking を漏らさず、Run の採番開始値から連番を割り当てる。"""

    context = _run_context(tmp_path, sequence_start=41)
    session_id = str(uuid4())
    mapper = ClaudeMessageMapper(context, session_id)
    events = mapper.map(
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="private reasoning", signature="sig"),
                TextBlock(text="completed text"),
                ToolUseBlock(
                    id="tool-1",
                    name="mcp__projectmind__issue_read_v1",
                    input={"issue_ref": "SECRET-1", "purpose": "analysis"},
                ),
            ],
            model="claude-test",
            session_id=session_id,
        )
    )

    assert [event.sequence for event in events] == [41, 42]
    assert [event.event_type for event in events] == [
        AgentEventType.TEXT_COMPLETED,
        AgentEventType.TOOL_REQUESTED,
    ]
    assert events[1].payload["input_keys"] == ["issue_ref", "purpose"]
    assert "SECRET-1" not in str(events)
    assert "private reasoning" not in str(events)


def test_message_mapper_maps_stream_result_and_mirror_degradation(tmp_path: Path) -> None:
    """Realtime delta、成功 Result、SessionStore 劣化を個別 event に変換する。"""

    context = _run_context(tmp_path)
    session_id = str(uuid4())
    mapper = ClaudeMessageMapper(context, session_id)

    delta_events = mapper.map(
        StreamEvent(
            uuid=str(uuid4()),
            session_id=session_id,
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a"}},
        )
    )
    mirror_events = mapper.map(
        MirrorErrorMessage(
            subtype="mirror_error",
            data={},
            error="database-password-must-not-leak",
        )
    )
    result_events = mapper.map(_result(session_id))

    assert delta_events[0].event_type is AgentEventType.TEXT_DELTA
    assert mirror_events[0].event_type is AgentEventType.SESSION_STORE_DEGRADED
    assert "database-password" not in str(mirror_events)
    assert [event.event_type for event in result_events] == [
        AgentEventType.USAGE_UPDATED,
        AgentEventType.RESULT_COMPLETED,
    ]


def test_message_mapper_uses_strict_json_result_as_compatibility_fallback(
    tmp_path: Path,
) -> None:
    """非公式 endpoint の raw result は JSON object の場合だけ structured output に昇格する。"""

    context = _run_context(tmp_path)
    session_id = str(uuid4())
    mapper = ClaudeMessageMapper(context, session_id)
    message = _result(session_id)
    message.structured_output = None
    message.result = json.dumps({"issue": {"id": "fixture-001"}})

    events = mapper.map(message)

    result = events[-1]
    assert result.event_type is AgentEventType.RESULT_COMPLETED
    assert result.payload["structured_output"] == {"issue": {"id": "fixture-001"}}
    assert result.payload["structured_output_source"] == "result_json_fallback"


def test_message_mapper_promotes_single_fenced_json_result(tmp_path: Path) -> None:
    """単一 code fence に包まれた厳密 JSON object は決定的に剥がして fallback 昇格する。"""

    context = _run_context(tmp_path)
    session_id = str(uuid4())
    mapper = ClaudeMessageMapper(context, session_id)
    message = _result(session_id)
    message.structured_output = None
    message.result = "```json\n" + json.dumps({"issue": {"id": "fixture-001"}}) + "\n```"

    result = mapper.map(message)[-1]

    assert result.event_type is AgentEventType.RESULT_COMPLETED
    assert result.payload["structured_output"] == {"issue": {"id": "fixture-001"}}
    assert result.payload["structured_output_source"] == "result_json_fallback"


def test_message_mapper_does_not_promote_markdown_result(tmp_path: Path) -> None:
    """fence 除去以外の Markdown 推測変換は行わず、validator が失敗を報告できる形を保つ。"""

    context = _run_context(tmp_path)
    session_id = str(uuid4())
    mapper = ClaudeMessageMapper(context, session_id)
    for raw in (
        "# Analysis report",
        # fence の中身が JSON でない場合は昇格しない。
        "```python\nprint('hello')\n```",
        # 前後に本文が付く fence は「単一 fence が全体を包む」条件を満たさない。
        "結果は以下です。\n```json\n{\"issue\": {}}\n```",
    ):
        message = _result(session_id)
        message.structured_output = None
        message.result = raw

        result = mapper.map(message)[-1]

        assert result.payload["structured_output"] is None
        assert result.payload["result"] == raw
    assert "structured_output_source" not in result.payload


@pytest.mark.asyncio
async def test_execute_owns_connect_receive_and_disconnect(tmp_path: Path) -> None:
    """新規 session は事前採番され、Result まで読んでから必ず close される。"""

    context = _run_context(tmp_path)
    factory = ClientFactory(_successful_messages)
    events = [event async for event in _engine(factory).execute(context)]
    client = factory.clients[0]

    assert client.connected and client.disconnected
    assert client.prompt == context.prompt
    assert client.options.session_id is not None
    assert client.options.resume is None
    assert [event.event_type for event in events] == [
        AgentEventType.SESSION_STARTED,
        AgentEventType.USAGE_UPDATED,
        AgentEventType.RESULT_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_resume_and_fork_set_sdk_session_options(tmp_path: Path) -> None:
    """Resume は同じ ID、Fork は parent と新規 ID の両方を SDK へ渡す。"""

    context = _run_context(tmp_path)
    parent = AgentSessionRef(
        run_id=context.run_id,
        run_attempt_id=uuid4(),
        session_id=str(uuid4()),
    )
    factory = ClientFactory(_successful_messages)
    engine = _engine(factory)

    _ = [
        event
        async for event in engine.resume(
            ResumeContext(run=context, session=parent, input_text="resume input")
        )
    ]
    _ = [
        event
        async for event in engine.fork(
            ForkContext(run=context, parent_session=parent, input_text="fork input")
        )
    ]

    resume_client, fork_client = factory.clients
    assert resume_client.options.resume == parent.session_id
    assert resume_client.options.session_id is None
    assert resume_client.prompt == "resume input"
    assert fork_client.options.resume == parent.session_id
    assert fork_client.options.session_id not in {None, parent.session_id}
    assert fork_client.options.fork_session is True


@pytest.mark.asyncio
async def test_interrupt_waits_for_result_drain_and_disconnect(tmp_path: Path) -> None:
    """Interrupt 呼び出しは receive loop の Result と disconnect 完了まで戻らない。"""

    context = _run_context(tmp_path)
    factory = ClientFactory(_successful_messages, wait_for_interrupt=True)
    engine = _engine(factory)

    collect_task = asyncio.create_task(_collect(engine.execute(context)))
    while not factory.clients:
        await asyncio.sleep(0)
    client = factory.clients[0]
    await client.receive_started.wait()
    assert client.options.session_id is not None
    session_ref = AgentSessionRef(
        run_id=context.run_id,
        run_attempt_id=context.run_attempt_id,
        session_id=client.options.session_id,
    )

    await engine.interrupt(session_ref)
    events = await collect_task

    assert client.interrupt_received.is_set()
    assert client.disconnected
    assert events[-1].event_type is AgentEventType.SESSION_INTERRUPTED


@pytest.mark.asyncio
async def test_missing_result_becomes_sanitized_engine_failure(tmp_path: Path) -> None:
    """SDK stream が Result なしで閉じても Run を成功扱いにしない。"""

    context = _run_context(tmp_path)
    factory = ClientFactory(
        lambda options: [
            SystemMessage(
                subtype="init",
                data={"session_id": _session_id(options), "model": "test"},
            )
        ]
    )

    events = [event async for event in _engine(factory).execute(context)]

    assert events[-1].event_type is AgentEventType.ENGINE_FAILED
    assert events[-1].payload == {"reason": "result_message_missing"}


async def _collect(iterator: AsyncIterator[Any]) -> list[Any]:
    """Async iterator を background task で最後まで消費する。"""

    return [item async for item in iterator]
