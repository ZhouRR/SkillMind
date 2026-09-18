"""遅い表示先が Agent 消費を止めず、所有 task と event 境界を維持する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.core.timing import ExecutionTimings
from skillmind.runs.realtime_buffer import BufferedRunRealtimePublisher


def event(sequence=1, *, kind=AgentEventType.TEXT_DELTA):
    """監査 ID は合成値とし、表示本文だけを持つ event を作る。"""
    return AgentEvent(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=str(uuid4()),
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        event_type=kind,
        payload={"text": str(sequence)},
    )


async def test_normal_delivery_preserves_sequence_and_detaches_payload():
    """正常時は順に配送し、元 dict の後続変更を反映しない。"""
    sink = AsyncMock()
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings())
    first = event()
    buffer.enqueue(first)
    first.payload["text"] = "changed"
    buffer.enqueue(event(2))
    await buffer.aclose()
    sent = [call.args[0] for call in sink.publish.await_args_list]
    assert [item.sequence for item in sent] == [1, 2]
    assert sent[0].payload["text"] == "1"
    assert buffer._task.done()


async def test_slow_sink_does_not_backpressure_engine_and_queue_is_bounded():
    """送信先が応答しなくても 1000 event の追加は I/O を待たずに完了する。"""
    blocked = asyncio.Event()
    sink = AsyncMock()

    async def wait_forever(_):
        """現在の publish を保留して queue の backpressure を再現する。"""
        await blocked.wait()

    sink.publish.side_effect = wait_forever
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings(), capacity=3, drain_seconds=0.01)
    for sequence in range(1, 1001):
        buffer.enqueue(event(sequence))
    assert buffer._queue.qsize() == 3
    await asyncio.sleep(0)
    await asyncio.wait_for(buffer.aclose(), timeout=0.5)
    assert buffer._task.done() and buffer._queue.empty()
    assert sink.publish.await_count == 1


async def test_notification_failure_does_not_fail_later_delivery():
    """表示故障を業務再試行にせず、次の通知を通常通り配送する。"""
    sink = AsyncMock()
    sink.publish.side_effect = [OSError("synthetic"), None]
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings())
    buffer.enqueue(event(1))
    buffer.enqueue(event(2))
    await buffer.aclose()
    assert sink.publish.await_count == 2


async def test_durable_events_cannot_enter_lossy_queue():
    """終態・批准などを drop 可能な通知として取り扱わない。"""
    sink = AsyncMock()
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings())
    try:
        with pytest.raises(ValueError):
            buffer.enqueue(event(kind=AgentEventType.RESULT_COMPLETED))
        assert buffer._queue.empty()
    finally:
        await buffer.aclose(drain=False)
    sink.publish.assert_not_awaited()


async def test_external_cancel_during_drain_propagates_after_cleanup():
    """取消を timeout や正常終了へ書き換えず、送信 task を残さない。"""
    entered = asyncio.Event()

    async def blocked(_):
        """待機中の I/O で取消を受ける。"""
        entered.set()
        await asyncio.Event().wait()

    sink = AsyncMock()
    sink.publish.side_effect = blocked
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings(), drain_seconds=1)
    buffer.enqueue(event())
    await entered.wait()
    closing = asyncio.create_task(buffer.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert buffer._task.done() and buffer._queue.empty()


async def test_cancelled_run_does_not_drain_pending_text():
    """取消後は残った表示 delta を送信しない。"""
    sink = AsyncMock()
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings())
    buffer.enqueue(event())
    await buffer.aclose(drain=False)
    sink.publish.assert_not_awaited()
    buffer.enqueue(event(2))
    assert buffer._queue.empty()


async def test_oversized_live_chunk_does_not_expand_buffer():
    """live delta の上限で原 message や結果の byte を変更しない。"""
    item = event()
    item.payload["text"] = "x" * 16_385
    sink = AsyncMock()
    buffer = BufferedRunRealtimePublisher(sink, ExecutionTimings())
    buffer.enqueue(item)
    await buffer.aclose()
    assert len(item.payload["text"]) == 16_385
    sink.publish.assert_not_awaited()
