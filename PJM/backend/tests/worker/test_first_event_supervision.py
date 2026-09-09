"""首 event 前の SDK connect/receive にも取消・lease・timeout の監督が届くことを検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from claude_agent_sdk.types import Message

from projectmind.agent.domain import AgentSessionRef
from projectmind.agent.result_validation import ResultValidator
from projectmind.runs.domain import LeaseValidationError, RunStatus
from projectmind.worker.executor import AgentRunExecutor
from tests.agent.test_claude_engine import (
    ClientFactory,
    ScriptedClaudeClient,
    _engine,
    _session_id,
    _successful_messages,
)
from tests.agent.test_result_validation import MemoryEvidenceLookup
from tests.worker.test_agent_run_executor import ContextBuilder, _claimed, _service


@pytest.mark.parametrize("phase", ["connect", "receive"])
@pytest.mark.parametrize("reason", ["user", "shutdown", "lease", "wall"])
async def test_first_event_wait_stops_and_awaits_client_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, reason: str
) -> None:
    """Session ID を未取得でも SDK await を止め、client 解放後だけ終態/取消を返す。"""

    started, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_connect = ScriptedClaudeClient.connect
    original_receive = ScriptedClaudeClient.receive_response
    original_disconnect = ScriptedClaudeClient.disconnect

    async def connect(client: ScriptedClaudeClient, prompt: str | None = None) -> None:
        """connect 内部で止まった場合も、外部 client の清理へ到達可能にする。"""

        await original_connect(client, prompt)
        if phase == "connect":
            started.set()
            await asyncio.Event().wait()

    async def receive(client: ScriptedClaudeClient) -> AsyncIterator[Message]:
        """最初の SESSION_STARTED を配送する前で止める。"""

        if phase == "receive":
            started.set()
            await asyncio.Event().wait()
        async for message in original_receive(client):
            yield message

    async def disconnect(client: ScriptedClaudeClient) -> None:
        """明示的に解放するまで、executor が戻ることや終態保存することを許さない。"""

        closing.set()
        await release.wait()
        await original_disconnect(client)

    monkeypatch.setattr(ScriptedClaudeClient, "connect", connect)
    monkeypatch.setattr(ScriptedClaudeClient, "receive_response", receive)
    monkeypatch.setattr(ScriptedClaudeClient, "disconnect", disconnect)
    factory = ClientFactory(_successful_messages)
    engine = _engine(factory)
    claimed, service = _claimed(), _service()
    executor = AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(
            tmp_path, wall_timeout_seconds=1 if reason == "wall" else 60
        ),
        engine=engine,
        result_validator=ResultValidator(MemoryEvidenceLookup(frozenset())),
        lease_seconds=60,
        heartbeat_interval_seconds=0.01,
    )
    execution = asyncio.create_task(executor.execute(claimed))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        if reason == "user":
            service.is_cancellation_requested.return_value = True
        elif reason == "lease":
            service.heartbeat_run_attempt.side_effect = LeaseValidationError("lost before event")
        elif reason == "shutdown":
            execution.cancel()
        await asyncio.wait_for(closing.wait(), timeout=2)
        assert not execution.done()
        service.finalize_execution.assert_not_awaited()
        service.append_agent_event.assert_not_awaited()
        release.set()
        if reason in {"shutdown", "lease"}:
            with pytest.raises(
                asyncio.CancelledError if reason == "shutdown" else LeaseValidationError
            ):
                await asyncio.wait_for(execution, timeout=2)
            service.finalize_execution.assert_not_awaited()
        else:
            await asyncio.wait_for(execution, timeout=2)
            result = service.finalize_execution.await_args.kwargs
            assert result["target"] is (
                RunStatus.CANCELLED if reason == "user" else RunStatus.FAILED
            )
            assert result["event"] is None
            if reason == "wall":
                assert result["error_json"]["code"] == "wall_timeout"
            else:
                assert result["error_json"] is None
        assert len(factory.clients) == 1
        assert factory.clients[0].disconnected
        with pytest.raises(LookupError, match="not active"):
            await engine.interrupt(
                AgentSessionRef(
                    claimed.run_id, claimed.run_attempt_id, _session_id(factory.clients[0].options)
                )
            )
    finally:
        release.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
