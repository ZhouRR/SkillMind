"""Codex catalog 準備の cold/warm 境界と失敗・取消時の観測を検証する。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.agent import codex_engine
from skillmind.agent.codex_engine import CodexAgentSdkEngine
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration
from skillmind.agent.domain import AgentEventType, AgentSessionRef, ResumeContext
from skillmind.agent.warm_codex import current_warm_session
from skillmind.core import timing
from tests.agent.test_session_store import MemoryTranscriptBackend
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry


@pytest.fixture
def engine_case(tmp_path, monkeypatch, caplog):
    """既存の memory port と合成 SDK を使い、実 process/network を起動しない。"""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(timing, "perf_counter", lambda: clock.now)
    caplog.set_level(logging.INFO, logger=timing.__name__)
    registry = _registry(CsvIssueProvider())
    context = replace(
        _context(tmp_path, registry), model="gpt-5.6-terra",
        result_schema={
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False,
        },
    )
    client = Mock()
    thread = SimpleNamespace(
        thread=SimpleNamespace(id=str(uuid4())), model=context.model,
        reasoning_effort=SimpleNamespace(value="max"),
    )
    client.thread_start.return_value = client.thread_resume.return_value = thread
    client.turn_start.return_value = SimpleNamespace(turn=SimpleNamespace(id="fixture-turn"))
    client.request.return_value = SimpleNamespace(
        model_dump=lambda **_: {"status": "unsubscribed"},
    )
    configuration = CodexRuntimeConfiguration(context.model, "max", tmp_path / "codex")
    prepared = object()

    async def prepare(*args, **kwargs):
        """Catalog/config の待機だけを固定 clock で進める。"""
        clock.now += 0.125
        return prepared

    def create(config):
        """Client 構築時間を別に進め、catalog 区間への混入を検出する。"""
        assert config is prepared
        clock.now += 10
        return client

    async def start(value):
        """SDK 起動は catalog 準備より後の別区間として模擬する。"""
        assert value is client
        assert _catalog_records(caplog)
        clock.now += 0.25

    bridges, endpoints = [], []

    @asynccontextmanager
    async def serve(bridge):
        """Attempt ごとの秘密 endpoint と cleanup を通信せず提供する。"""
        bridges.append(bridge)
        endpoint = {"url": f"http://127.0.0.1/private-fixture-{len(bridges)}"}
        endpoints.append(endpoint)
        try:
            yield endpoint
        finally:
            bridge.closed.set()

    async def notifications(value, turn_id):
        """保存済み terminal を持つ正常 turn を決定的に返す。"""
        assert value is client and turn_id == "fixture-turn"
        yield {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "message", "text": '{"ok":true}'}},
        }
        yield {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}

    prepare_mock, create_mock = AsyncMock(side_effect=prepare), Mock(side_effect=create)
    start_mock = AsyncMock(side_effect=start)
    monkeypatch.setattr(codex_engine, "prepare_codex_config", prepare_mock)
    monkeypatch.setattr(codex_engine, "create_codex_client", create_mock)
    monkeypatch.setattr(codex_engine, "start_codex", start_mock)
    monkeypatch.setattr(codex_engine, "serve_codex_tools", serve)
    monkeypatch.setattr(codex_engine, "codex_notifications", notifications)
    engine = CodexAgentSdkEngine(
        configuration=configuration,
        runtime_factory=lambda run: registry.build_gateway_runtime(
            run, audit_writer=MemoryAuditWriter(),
        ),
        transcript_backend=MemoryTranscriptBackend(),
    )
    return SimpleNamespace(
        engine=engine, context=context, configuration=configuration, client=client,
        prepare=prepare_mock, create=create_mock, start=start_mock, clock=clock,
        bridges=bridges, endpoints=endpoints,
    )


def _catalog_records(caplog):
    """実 production logging allowlist を通過した catalog 記録だけを選ぶ。"""
    return [r for r in caplog.records if r.getMessage() == "run.performance.catalog_prepare"]


def _assert_catalog_record(caplog, context):
    """ID と時間だけを一度記録し、本文・endpoint・例外値を漏らさない。"""
    records = _catalog_records(caplog)
    assert len(records) == 1
    assert records[0].skillmind_context == {
        "run_id": str(context.run_id), "run_attempt_id": str(context.run_attempt_id),
        "duration_ms": 125.0,
    }


async def test_cold_catalog_timing_precedes_and_excludes_sdk_start(engine_case, caplog):
    """Cold path は準備一回を記録し、client 構築と SDK 起動を合算しない。"""
    case = engine_case
    events = [event async for event in case.engine.execute(case.context)]
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED
    assert events[-1].payload["structured_output"] == {"ok": True}
    case.prepare.assert_awaited_once_with(case.configuration, mcp=case.endpoints[0])
    case.create.assert_called_once()
    case.start.assert_awaited_once_with(case.client)
    case.client.close.assert_called_once()
    _assert_catalog_record(caplog, case.context)
    phases = [r.getMessage() for r in caplog.records]
    assert phases[:3] == [
        "run.performance.catalog_prepare", "run.performance.sdk_reuse", "run.performance.sdk_start",
    ]
    assert caplog.records[2].skillmind_context["duration_ms"] == 250.0


async def test_warm_resume_does_not_prepare_or_time_catalog_again(engine_case, caplog):
    """確認済み親の暖続行は新 Gateway を使い、catalog と SDK 起動を再計測しない。"""
    case = engine_case
    async with case.engine.continuation_scope(case.context.run_id):
        first = [event async for event in case.engine.execute(case.context)]
        session = AgentSessionRef(
            case.context.run_id, case.context.run_attempt_id, first[0].agent_session_id,
        )
        following = replace(case.context, run_attempt_id=uuid4())
        second = [event async for event in case.engine.resume(ResumeContext(following, session))]
        assert first[-1].event_type is second[-1].event_type is AgentEventType.RESULT_COMPLETED
        case.client.close.assert_not_called()
    case.client.close.assert_called_once()
    case.prepare.assert_awaited_once_with(case.configuration, mcp=None)
    case.create.assert_called_once()
    case.start.assert_awaited_once_with(case.client)
    _assert_catalog_record(caplog, case.context)
    assert case.client.request.call_count == 2
    assert case.client.thread_resume.call_args.args[0] == session.session_id
    assert case.client.thread_resume.call_args.args[1]["config"] == {
        "mcp_servers": {"skillmind": case.endpoints[1]},
    }
    assert case.endpoints[0] != case.endpoints[1]
    reuse = [r.skillmind_context for r in caplog.records
             if r.getMessage() == "run.performance.sdk_reuse"]
    assert [r["status"] for r in reuse] == ["new", "reused"]
    assert reuse[1]["run_attempt_id"] == str(following.run_attempt_id)
    assert sum(r.getMessage() == "run.performance.sdk_start" for r in caplog.records) == 1


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("broken_log", [False, True])
async def test_catalog_failure_or_cancel_preserves_cleanup_and_telemetry(
    engine_case, caplog, monkeypatch, warm, cancel, broken_log,
):
    """準備の例外・Task 取消は原型を保持し、観測障害でも SDK を開始しない。"""
    case = engine_case
    entered = asyncio.Event()
    failures = []

    async def prepare(*args, **kwargs):
        """同期例外と実 Task 取消の双方を準備境界で発生させる。"""
        case.clock.now += 0.125
        entered.set()
        try:
            if cancel:
                await asyncio.Event().wait()
            raise ValueError("private-fixture-error")
        except BaseException as error:
            failures.append(error)
            raise

    def failed_log(*args, **kwargs):
        """観測先の故障は原失敗を上書きしてはならない。"""
        raise OSError("fixture log unavailable")

    case.prepare.side_effect = prepare
    if broken_log:
        monkeypatch.setattr(timing, "log_event", failed_log)
    scope = case.engine.continuation_scope(case.context.run_id) if warm else nullcontext()
    async with scope:
        pending = asyncio.create_task(anext(case.engine.execute(case.context)))
        await entered.wait()
        if cancel:
            pending.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError) as caught:
            await pending
        assert caught.value is failures[0]
        if warm:
            holder = current_warm_session(case.context.run_id, case.engine)
            assert holder is not None and holder.client is None and not holder.busy
    case.prepare.assert_awaited_once()
    case.create.assert_not_called()
    case.start.assert_not_awaited()
    assert case.bridges[0].closed.is_set() and not case.engine._active
    if broken_log:
        assert not caplog.records
    else:
        _assert_catalog_record(caplog, case.context)
        assert len(caplog.records) == 1
