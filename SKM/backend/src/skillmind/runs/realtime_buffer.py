"""表示専用 TEXT_DELTA を有界 queue に移し、モデル消費を Redis 待機から分離する。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.core.timing import ExecutionTimings
from skillmind.runs.realtime import RunRealtimePublisher


class BufferedRunRealtimePublisher:
    """一 Attempt が所有する送信 task。監査・批准・Effect・終態を queue に入れない。"""

    def __init__(
        self,
        publisher: RunRealtimePublisher,
        timings: ExecutionTimings,
        *,
        capacity: int = 32,
        drain_seconds: float = 0.1,
    ) -> None:
        """小さい queue と終了時の待機上限を固定し、Run 間で task を共有しない。"""
        if capacity < 1 or drain_seconds <= 0:
            raise ValueError("Realtime buffer limits must be positive")
        self._publisher = publisher
        self._timings = timings
        self._queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=capacity)
        self._drain_seconds = drain_seconds
        self._closed = False
        self._task = asyncio.create_task(self._deliver(), name="run-realtime-delivery")

    def enqueue(self, event: AgentEvent) -> None:
        """本文は最終 message に保存されるため、遅い表示先では delta だけを落とせる。"""
        text = event.payload.get("text")
        if (
            event.event_type is not AgentEventType.TEXT_DELTA
            or not isinstance(text, str)
            or not text
        ):
            raise ValueError("Realtime buffer accepts nonempty TEXT_DELTA only")
        if self._closed or self._task.done() or len(text) > 16_384:
            return
        # 32 * 16K characters で使用量を限定。元 payload を遅延送信中に所有しない。
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(replace(event, payload=dict(event.payload)))

    async def _deliver(self) -> None:
        """成功・失敗に関わらず queue を精算し、取消だけは上位の cleanup に伝える。"""
        while True:
            event = await self._queue.get()
            try:
                with self._timings.measure("realtime_publish"):
                    await self._publisher.publish(event)
            except Exception:
                # 表示 adapter の故障は本体 transaction や model を失敗・再実行させない。
                pass
            finally:
                self._queue.task_done()

    async def aclose(self, *, drain: bool = True) -> None:
        """正常終了でも短く待つだけとし、取消時は待たず所有 task を必ず回収する。"""
        if self._closed:
            return
        self._closed = True
        try:
            if drain and not self._task.done():
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._drain_seconds):
                        await self._queue.join()
        finally:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            # 送信されなかった表示本文を保持し続けない。
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
