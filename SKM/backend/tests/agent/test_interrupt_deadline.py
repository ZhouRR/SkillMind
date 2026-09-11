"""中断要求と drain が一つの期限を共有することを検証する。実 process 停止は別証拠。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.domain import AgentSessionRef
from skillmind.agent.engine import ClaudeAgentSdkEngine, ClaudeClient, _ActiveExecution


async def active_engine(
    *, timeout: float = 0.02,
) -> tuple[ClaudeAgentSdkEngine, _ActiveExecution, MagicMock]:
    """受信 loop の進み方に依存せず、登録した一 client の中断期限だけを検査する。"""
    engine = ClaudeAgentSdkEngine(
        mcp_server_factory=lambda _context: {},
        configuration=ClaudeRuntimeConfiguration(environment={}),
        interrupt_drain_timeout_seconds=timeout,
    )
    client = MagicMock(spec=ClaudeClient)
    active = _ActiveExecution(AgentSessionRef(uuid4(), uuid4(), str(uuid4())), client)
    await engine._register(active)
    return engine, active, client


@pytest.mark.parametrize("stage", ["request", "drain", "swallowed_deadline"])
async def test_interrupt_deadline_covers_request_and_drain(stage: str) -> None:
    """要求自体の無応答や期限取消しの握潰しでも、最後の disconnect に到達する。"""
    engine, active, client = await active_engine()
    request_cancelled = asyncio.Event()

    async def request() -> None:
        """drain case 以外は制御応答を保留し、期限取消しの伝播も観測する。"""
        if stage == "drain":
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            request_cancelled.set()
            if stage != "swallowed_deadline":
                raise

    client.interrupt = AsyncMock(side_effect=request)
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError, match="Claude SDK interrupt drain timed out"):
            await engine.interrupt(active.session_ref)
    client.disconnect.assert_awaited_once()
    assert request_cancelled.is_set() is (stage != "drain")
    assert active.interrupt_requested
    assert not active.done.is_set()


@pytest.mark.parametrize("swallow", [False, True])
async def test_caller_cancellation_is_not_changed_to_timeout_or_success(swallow: bool) -> None:
    """依存先が取消しを捕えても元の取消しを返し、停止確認を補造しない。"""
    engine, active, client = await active_engine(timeout=30)
    entered = asyncio.Event()

    async def request() -> None:
        """呼出し側による取消しを受けるまで、制御要求を進めない。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not swallow:
                raise

    client.interrupt = AsyncMock(side_effect=request)
    task = asyncio.create_task(engine.interrupt(active.session_ref))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.disconnect.assert_not_awaited()
        assert not active.done.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_completed_drain_returns_without_an_extra_disconnect() -> None:
    """所有 receive loop が cleanup 済みなら、中断側から再 close しない。"""
    engine, active, client = await active_engine()
    client.interrupt = AsyncMock(side_effect=active.done.set)
    await engine.interrupt(active.session_ref)
    client.interrupt.assert_awaited_once()
    client.disconnect.assert_not_awaited()


@pytest.mark.parametrize("swallow", [False, True])
async def test_cancellation_during_timeout_cleanup_remains_cancellation(swallow: bool) -> None:
    """期限後 close が取消しを捕えても、元の timeout へすり替えない。"""
    engine, active, client = await active_engine()
    cleaning = asyncio.Event()

    async def disconnect() -> None:
        """期限後 cleanup の中で呼出し側の取消しを受ける。"""
        cleaning.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not swallow:
                raise

    client.disconnect = AsyncMock(side_effect=disconnect)
    task = asyncio.create_task(engine.interrupt(active.session_ref))
    try:
        async with asyncio.timeout(1):
            await cleaning.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not active.done.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_control_request_attempts_disconnect_without_claiming_done(
    cleanup_fails: bool,
) -> None:
    """中断要求が失敗しても close を試し、二重失敗を停止済みと扱わない。"""
    engine, active, client = await active_engine(timeout=30)
    request_error = RuntimeError("fixture control failure")
    close_error = RuntimeError("fixture cleanup failure")
    client.interrupt = AsyncMock(side_effect=request_error)
    if cleanup_fails:
        client.disconnect = AsyncMock(side_effect=close_error)
    with pytest.raises(RuntimeError) as caught:
        await engine.interrupt(active.session_ref)
    assert caught.value is (close_error if cleanup_fails else request_error)
    client.disconnect.assert_awaited_once()
    assert not active.done.is_set()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), 0, -1])
async def test_interrupt_deadline_rejects_nonfinite_or_nonpositive_timeout(
    timeout: float,
) -> None:
    """無限値や NaN を受けて中断要求の期限が無効化されることを防ぐ。"""
    with pytest.raises(ValueError, match="finite and positive"):
        await active_engine(timeout=timeout)
