"""実 registry/Gateway まで通し、子の終端と SDK 所有者の清理を検証する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from claude_agent_sdk.types import Message, SystemMessage

from skillmind.agent.context_builder import ContractStore, _subagent_tool_definitions
from skillmind.agent.domain import AgentEvent, AgentEventType, RunContext
from skillmind.agent.subagent_provider import SubagentDispatchProvider
from skillmind.agent.tool_gateway import RunToolRuntime, ToolRegistry
from tests.agent.test_claude_engine import ClientFactory, _engine, _result, _session_id
from tests.agent.test_subagent_provider import (
    _parent_context,
    _provider,
    _RecordingRecorder,
    _request,
    _tool_context,
)
from tests.agent.test_tool_gateway import MemoryAuditWriter

ROOT = Path(__file__).resolve().parents[3]


class EventEngine:
    """指定された event 列を一つの実行 identity で返す fake engine。"""

    def __init__(
        self,
        events: Sequence[AgentEventType],
        *,
        transform: Callable[[AgentEvent], AgentEvent] | None = None,
    ) -> None:
        """終端・衝突・欠落の順序と cleanup の観測点を保持する。"""

        self.events = events
        self.transform = transform
        self.closed = False
        self.calls = 0

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """partial text があっても成功終端の代わりにならないことを再現する。"""

        session_id = str(uuid4())
        self.calls += 1
        try:
            for sequence, event_type in enumerate(self.events, start=1):
                event = AgentEvent(
                    run_id=context.run_id,
                    run_attempt_id=context.run_attempt_id,
                    agent_session_id=session_id,
                    sequence=sequence,
                    occurred_at=datetime.now(UTC),
                    event_type=event_type,
                    payload={"text": "partial", "structured_output": {"summary": "verified"}},
                )
                yield self.transform(event) if self.transform is not None else event
        finally:
            self.closed = True


def _gateway(
    parent: RunContext, provider: SubagentDispatchProvider, writer: MemoryAuditWriter
) -> tuple[RunToolRuntime, str]:
    """成功/失敗の両試験が同じ実 registry と権限 snapshot を使うよう組み立てる。"""

    registry = ToolRegistry(_subagent_tool_definitions(ContractStore(ROOT / "contracts"), provider))
    registered = registry.resolve_unbound("subagent.dispatch/v1", execution_profile="SUPERVISED")
    context = replace(
        parent,
        tools=(registered,),
        permission_snapshot={
            "allowed_capabilities": ["subagent.dispatch/v1"],
            "execution_profile": "SUPERVISED",
            "mode": "auto_read_only",
        },
    )
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    return runtime, registered.sdk_name


async def _invoke(
    parent: RunContext, provider: SubagentDispatchProvider
) -> tuple[dict[str, Any], MemoryAuditWriter]:
    """本番の登録定義と PreToolUse/Gateway を省略せず、成功した監査結果を返す。"""

    writer = MemoryAuditWriter()
    runtime, tool_name = _gateway(parent, provider, writer)
    arguments = _request("a")
    assert runtime.mcp.on_tool_authorized is not None
    await runtime.mcp.on_tool_authorized(tool_name, arguments, "call-1", str(uuid4()))
    response = await runtime.gateway.invoke_mcp(tool_name, arguments)
    assert not response.get("is_error", False), response
    lease = next(iter(writer.by_use_id.values()))
    assert lease.result is not None
    return dict(lease.result), writer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [],
        [AgentEventType.TEXT_COMPLETED],
        [AgentEventType.ENGINE_FAILED],
        [AgentEventType.SESSION_INTERRUPTED],
        [AgentEventType.SESSION_DEFERRED],
        [AgentEventType.INTERACTION_REQUESTED],
        [AgentEventType.CHANGE_PROPOSED],
        [AgentEventType.RESULT_COMPLETED, AgentEventType.RESULT_COMPLETED],
        [AgentEventType.RESULT_COMPLETED, AgentEventType.ENGINE_FAILED],
        [AgentEventType.RESULT_COMPLETED, AgentEventType.TEXT_DELTA],
    ],
)
async def test_non_success_or_conflicting_stream_cannot_complete(
    tmp_path: Path, events: list[AgentEventType]
) -> None:
    """失敗・禁止 defer・成功欠落/衝突を completed Evidence に変換しない。"""

    engine = EventEngine(events)
    recorder = _RecordingRecorder()
    provider = _provider(engine=lambda: engine, session_recorder=recorder)
    response, writer = await _invoke(_parent_context(tmp_path), provider)

    assert response["results"][0]["outcome"] == "FAILED"
    assert response["results"][0]["summary"] == ""
    assert response["results"][0]["failure_code"] == "branch_failed"
    assert recorder.drafts[0].outcome == "FAILED"
    assert writer.completed[0][1][0].draft.metadata["branches"][0]["outcome"] == "FAILED"
    assert engine.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_actual_sdk_mapper_terminal_reaches_gateway(tmp_path: Path, is_error: bool) -> None:
    """実 mapper の ResultMessage と例外ではない error を Provider/Gateway へ接続する。"""

    def messages(options) -> list[Message]:
        """外部 SDK subprocess を起動せず、同じ SDK 型の終端を返す。"""

        session_id = _session_id(options)
        return [
            SystemMessage(subtype="init", data={"session_id": session_id}),
            _result(session_id, is_error=is_error),
        ]

    factory = ClientFactory(messages)
    engine = _engine(factory)
    recorder = _RecordingRecorder()
    provider = _provider(engine=lambda: engine, session_recorder=recorder)
    response, _ = await _invoke(_parent_context(tmp_path), provider)

    result = response["results"][0]
    assert result["outcome"] == ("FAILED" if is_error else "COMPLETED")
    assert result["summary"] == ("" if is_error else "ok")
    assert recorder.drafts[0].sdk_session_id == UUID(_session_id(factory.clients[0].options))
    assert factory.clients[0].disconnected


@pytest.mark.parametrize("length", [300, 1500, 20_000, 20_001])
async def test_branch_summary_is_not_a_run_list_preview(tmp_path: Path, length: int) -> None:
    """検証済み branch 本文を一覧用の 280 文字に切らず、Tool 契約の上限だけを使う。"""

    summary = "読" * length
    engine = EventEngine(
        [AgentEventType.RESULT_COMPLETED],
        transform=lambda event: replace(event, payload={"structured_output": {"summary": summary}}),
    )
    response, _ = await _invoke(_parent_context(tmp_path), _provider(engine=lambda: engine))
    assert response["results"][0]["summary"] == summary[:20_000]


@pytest.mark.parametrize(
    "output",
    [
        None,
        [],
        {},
        {"summary": 1},
        {"summary": "ok", "api_key": "fixture"},
        {"summary": "ok", "evidence_refs": ["ev_unowned"]},
    ],
)
async def test_invalid_structured_result_cannot_become_completed_evidence(
    tmp_path: Path, output: Any
) -> None:
    """SDK success があっても構造・機密・Evidence 所有を実 validator/Gateway で拒否する。"""

    parent = replace(
        _parent_context(tmp_path),
        result_schema={
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    )
    engine = EventEngine(
        [AgentEventType.RESULT_COMPLETED],
        transform=lambda event: replace(event, payload={"structured_output": output}),
    )
    response, writer = await _invoke(parent, _provider(engine=lambda: engine))
    assert response["results"][0]["outcome"] == "FAILED"
    assert response["results"][0]["summary"] == ""
    assert writer.completed[0][1][0].draft.metadata["branches"][0]["outcome"] == "FAILED"
    assert engine.closed


@pytest.mark.parametrize(
    "invalid", ["run", "attempt", "session", "session_format", "sequence", "sequence_start"]
)
async def test_foreign_or_reordered_event_cannot_complete(tmp_path: Path, invalid: str) -> None:
    """親実行の identity と一支内の sequence のどちらが違っても成功監査を作らない。"""

    def transform(event: AgentEvent) -> AgentEvent:
        """二件目だけを壊して、最初に観測した正しい Session を監査へ残せるようにする。"""

        if event.sequence != 2:
            return event
        replacements = {
            "run": {"run_id": uuid4()},
            "attempt": {"run_attempt_id": uuid4()},
            "session": {"agent_session_id": str(uuid4())},
            "session_format": {"agent_session_id": "invalid-session"},
            "sequence": {"sequence": 1},
        }
        return replace(event, **replacements.get(invalid, {}))

    parent = _parent_context(tmp_path)
    if invalid == "sequence_start":
        parent = replace(parent, sequence_start=10)
    engine = EventEngine(
        [AgentEventType.SESSION_STARTED, AgentEventType.RESULT_COMPLETED], transform=transform
    )
    response, _ = await _invoke(parent, _provider(engine=lambda: engine))
    assert response["results"][0]["outcome"] == "FAILED"
    assert engine.closed


async def test_session_save_failure_is_a_gateway_error_without_reexecution(tmp_path: Path) -> None:
    """Session 監査の失敗を実 Gateway の error へ投影し、欠落 ID の成功応答を作らない。"""

    engine = EventEngine([AgentEventType.RESULT_COMPLETED])
    recorder = _RecordingRecorder(fail=True)
    provider = _provider(engine=lambda: engine, session_recorder=recorder)
    writer = MemoryAuditWriter()
    runtime, tool_name = _gateway(_parent_context(tmp_path), provider, writer)
    assert runtime.mcp.on_tool_authorized is not None
    arguments = _request("a")
    await runtime.mcp.on_tool_authorized(tool_name, arguments, "call-1", str(uuid4()))
    result = await runtime.gateway.invoke_mcp(tool_name, arguments)
    assert result["is_error"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "unavailable" and payload["retryable"] is False
    assert engine.calls == recorder.calls == 1
    assert not writer.completed
    assert writer.failed[0][1:] == ("unavailable", False)


class ClosingEngine:
    """二支の cleanup を別々に開放し、親が片方の清理を再取消しないか観測する。"""

    def __init__(self) -> None:
        """実行開始と清理開始を sleep の偶然に依存しない event として保持する。"""

        self.started = {key: asyncio.Event() for key in ("a", "b")}
        self.closing = {key: asyncio.Event() for key in ("a", "b")}
        self.release = {key: asyncio.Event() for key in ("a", "b")}
        self.closed: list[str] = []
        self.interrupted: list[str] = []

    async def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """取消時の finally を別 branch の終了で中断されたら記録する。"""

        key = str(context.permission_snapshot["subagent_branch"])
        self.started[key].set()
        try:
            await asyncio.Event().wait()
            yield  # pragma: no cover
        finally:
            self.closing[key].set()
            try:
                await self.release[key].wait()
                self.closed.append(key)
            except asyncio.CancelledError:
                self.interrupted.append(key)
                raise


async def test_cancellation_waits_for_every_branch_cleanup(tmp_path: Path) -> None:
    """先に清理できた支の CancelledError で、残りの清理を二度取消してはならない。"""

    engine = ClosingEngine()
    provider = _provider(engine=lambda: engine)
    task = asyncio.create_task(
        provider.execute(_tool_context(_parent_context(tmp_path)), _request("a", "b"))
    )
    try:
        async with asyncio.timeout(2):
            await engine.started["a"].wait()
            await engine.started["b"].wait()
            task.cancel()
            await engine.closing["a"].wait()
            await engine.closing["b"].wait()
            engine.release["a"].set()
            # a の終了 callback と親の finally を進め、b の清理が再取消される機会を作る。
            for _ in range(10):
                await asyncio.sleep(0)
            engine.release["b"].set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert sorted(engine.closed) == ["a", "b"]
        assert not engine.interrupted
    finally:
        for release in engine.release.values():
            release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
