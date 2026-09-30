"""合成時計と memory event で SSE replay の速度・復旧・終態境界を検証する。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from skillmind.api.routes import realtime
from skillmind.runs.domain import RunStatus, StoredRunEvent


class ReplayHarness:
    """実 DB/Redis へ接続せず、idle wait と返却 page を決定的に記録する。"""

    def __init__(self, count: int, *, finished: bool = True, redis_failure: str = "") -> None:
        self.run_id = uuid4()
        self.now = 10.0
        self.idle_wait = 0.0
        self.yields = 0
        self.polls: list[int] = []
        self.closed = False
        self.finished = finished
        self.redis_failure = redis_failure
        self.messages: list[dict[str, Any]] = []
        self.events = [self.event(index) for index in range(1, count + 1)]
        self.arrivals: list[StoredRunEvent] = []
        self.disconnect = False

    def event(
        self, sequence: int, event_type: str = "SESSION_STARTED", **payload: Any,
    ) -> StoredRunEvent:
        """公開 DTO と同じ形状の synthetic event を返す。"""
        return StoredRunEvent(self.run_id, None, None, sequence, event_type,
                              datetime(2026, 1, 1, tzinfo=UTC), payload, None)

    async def list_events(self, run_id: Any, *, after: int) -> list[StoredRunEvent]:
        """実 repository の昇順・100 件 page を再現する。"""
        assert run_id == self.run_id
        self.polls.append(after)
        page = [event for event in self.events if event.sequence > after][:100]
        if len(self.polls) == 1:
            self.events.extend(self.arrivals)
        return page

    async def subscribe(self, channel: str) -> None:
        """購読失敗の fallback を合成する。"""
        if self.redis_failure == "subscribe":
            raise RedisError("synthetic unavailable")

    async def get_message(self, **kwargs: Any) -> dict[str, Any] | None:
        """Redis 待機を実 sleep なしで計測する。"""
        if self.redis_failure == "read":
            raise RedisError("synthetic unavailable")
        if self.messages:
            return self.messages.pop(0)
        await self.sleep(kwargs["timeout"])
        return None

    async def sleep(self, delay: float) -> None:
        """協調 yield と polling 待機を区別する。"""
        self.now += delay
        self.idle_wait += delay
        self.yields += delay == 0

    async def aclose(self) -> None:
        """generator の finally による解放を記録する。"""
        self.closed = True

    async def is_disconnected(self) -> bool:
        """非終態 test も有限時間で終了し、busy loop を検出する。"""
        assert len(self.polls) < 100, "unbounded database polling"
        return self.disconnect or self.idle_wait >= 3

    async def collect(self, monkeypatch: pytest.MonkeyPatch, *, after: int = 0,
                      last_event_id: str | None = None) -> list[str]:
        """認可済み route generator を wire format まで通す。"""
        async def authorized(*args: Any) -> Any:
            """認可の business 境界は既存 API test に委ねる。"""
            status = RunStatus.SUCCEEDED if self.finished else RunStatus.RUNNING
            return SimpleNamespace(status=status)

        monkeypatch.setattr(realtime, "authorized_run", authorized)
        monkeypatch.setattr(realtime, "monotonic", lambda: self.now)
        monkeypatch.setattr(realtime.asyncio, "sleep", self.sleep)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(
                settings=SimpleNamespace(sse_heartbeat_seconds=1),
                run_service=self, redis=SimpleNamespace(pubsub=lambda: self))),
            is_disconnected=self.is_disconnected,
        )
        response = await realtime.run_events(request, self.run_id, None, after, last_event_id)
        return [chunk async for chunk in response.body_iterator]


def records(chunks: list[str]) -> list[dict[str, Any]]:
    """Heartbeat を除外し、wire data の順序と内容を検証可能にする。"""
    return [json.loads(chunk.split("data: ", 1)[1]) for chunk in chunks if "data: " in chunk]


@pytest.mark.parametrize("count", [0, 100, 101, 1000])
@pytest.mark.parametrize("redis_failure", ["", "subscribe", "read"])
def test_finished_backlog_drains_without_idle_wait(
    monkeypatch: pytest.MonkeyPatch, count: int, redis_failure: str,
) -> None:
    """満杯 page 間に polling 待機を入れず、全 event 配信後に解放する。"""
    harness = ReplayHarness(count, redis_failure=redis_failure)
    chunks = asyncio.run(harness.collect(monkeypatch))
    assert [event["sequence"] for event in records(chunks)] == list(range(1, count + 1))
    assert harness.closed
    assert harness.idle_wait == 0
    assert len(harness.polls) == (count + 99) // 100 + 1
    assert harness.yields == (count + 99) // 100


def test_replay_includes_arrival_and_terminal_snapshot_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain 中の追加も最後の終態 snapshot まで欠落なく配信する。"""
    harness = ReplayHarness(100, finished=False)
    harness.arrivals = [harness.event(103, "TEXT_COMPLETED", text="full text"),
                        harness.event(104, "RUN_SNAPSHOT", status="SUCCEEDED")]
    chunks = asyncio.run(harness.collect(monkeypatch))
    values = records(chunks)
    assert [event["sequence"] for event in values] == [*range(1, 101), 103, 104]
    assert values[-2]["payload"]["text"] == "full text"
    assert values[-1]["event_type"] == "RUN_SNAPSHOT"
    assert harness.idle_wait == 0
    assert harness.closed


def test_last_event_id_and_query_preserve_durable_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """再接続 header/query の大きい cursor を採用し、欠番を許容する。"""
    harness = ReplayHarness(101)
    chunks = asyncio.run(harness.collect(monkeypatch, after=98, last_event_id="100"))
    assert [event["sequence"] for event in records(chunks)] == [101]
    assert chunks[0].startswith("id: 101\n")
    assert harness.polls == [100, 101]


def test_idle_stream_retains_delta_without_persistent_id_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """追随後は 1 秒 polling と heartbeat を維持し、一時 delta は replay ID にしない。"""
    harness = ReplayHarness(1, finished=False)
    harness.messages = [{"data": json.dumps({
        "run_id": str(harness.run_id), "sequence": 3,
        "event_type": "TEXT_DELTA", "payload": {"text": "partial"},
    })}]
    chunks = asyncio.run(harness.collect(monkeypatch))
    assert [event["sequence"] for event in records(chunks)] == [1, 3]
    assert chunks[1].startswith("event: text.delta\n")
    assert any(chunk.startswith(": heartbeat") for chunk in chunks)
    assert harness.polls == [0, 1, 1, 1]
    assert harness.closed


def test_disconnect_between_pages_closes_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    """大きい backlog でも page 境界で切断を観測して購読を解放する。"""
    harness = ReplayHarness(1000)
    original = harness.list_events

    async def disconnect_after_page(run_id: Any, *, after: int) -> list[StoredRunEvent]:
        """最初の page 取得時に切断を発生させる。"""
        page = await original(run_id, after=after)
        harness.disconnect = True
        return page

    harness.list_events = disconnect_after_page  # type: ignore[method-assign]
    chunks = asyncio.run(harness.collect(monkeypatch))
    assert len(records(chunks)) == 100
    assert harness.closed


@pytest.mark.parametrize("redis_failure", ["subscribe", "read"])
def test_idle_redis_failure_keeps_polling_and_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch, redis_failure: str,
) -> None:
    """idle 中の Redis 障害後も DB の完成本文と最後の snapshot を回収する。"""
    harness = ReplayHarness(0, finished=False, redis_failure=redis_failure)
    original_sleep = harness.sleep

    async def arrive_after_wait(delay: float) -> None:
        """最初の fallback wait で永続結果が到着する。"""
        await original_sleep(delay)
        if delay > 0 and not harness.events:
            harness.events = [harness.event(2, "TEXT_COMPLETED", text="recovered full text"),
                              harness.event(3, "RUN_SNAPSHOT", status="FAILED")]

    harness.sleep = arrive_after_wait  # type: ignore[method-assign]
    chunks = asyncio.run(harness.collect(monkeypatch))
    values = records(chunks)
    assert [event["sequence"] for event in values] == [2, 3]
    assert values[0]["payload"]["text"] == "recovered full text"
    assert harness.idle_wait == 1
    assert harness.closed


def test_backlog_precedes_delta_and_terminal_remains_last(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis 待機中の古い delta/終態後の通知を durable replay の先へ出さない。"""
    harness = ReplayHarness(101, finished=False)
    harness.events += [harness.event(104, "TEXT_COMPLETED", text="full"),
                       harness.event(105, "RUN_SNAPSHOT", status="CANCELLED")]
    harness.messages = [{"data": json.dumps({
        "run_id": str(harness.run_id), "sequence": sequence,
        "event_type": "TEXT_DELTA", "payload": {"text": "stale"},
    })} for sequence in (102, 106)]
    chunks = asyncio.run(harness.collect(monkeypatch))
    values = records(chunks)
    assert [event["sequence"] for event in values] == [*range(1, 102), 104, 105]
    assert values[-1]["event_type"] == "RUN_SNAPSHOT"
    assert harness.closed
