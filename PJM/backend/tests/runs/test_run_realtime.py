"""Run realtime Redis transport の隔離と公開 payload を検証する。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEvent, AgentEventType
from projectmind.runs.realtime import RedisRunRealtimePublisher, run_realtime_channel


class FakeRedisPublisher:
    """Publish 引数を記録する Redis test double。"""

    def __init__(self) -> None:
        """空の publish 履歴を初期化する。"""

        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        """Channel と message を記録して subscriber 数を返す。"""

        self.published.append((channel, message))
        return 1


def _event(event_type: AgentEventType, payload: dict[str, object]) -> AgentEvent:
    """Realtime publisher test 用の AgentEvent を生成する。"""

    return AgentEvent(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=str(uuid4()),
        sequence=12,
        occurred_at=datetime(2026, 7, 2, 13, 0, tzinfo=UTC),
        event_type=event_type,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_text_delta_is_published_to_run_scoped_channel() -> None:
    """TEXT_DELTA が別 Run と混ざらない channel と public shape で配送される。"""

    redis = FakeRedisPublisher()
    event = _event(AgentEventType.TEXT_DELTA, {"text": "partial"})

    await RedisRunRealtimePublisher(redis).publish(event)

    assert redis.published[0][0] == run_realtime_channel(event.run_id)
    payload = json.loads(redis.published[0][1])
    assert payload["event_type"] == "TEXT_DELTA"
    assert payload["payload"] == {"text": "partial"}
    assert payload["sequence"] == 12


@pytest.mark.asyncio
async def test_publisher_rejects_non_delta_event() -> None:
    """永続 event を誤って一時 channel だけへ送る実装ミスを拒否する。"""

    redis = FakeRedisPublisher()
    event = _event(AgentEventType.TEXT_COMPLETED, {"text": "complete"})

    with pytest.raises(ValueError, match="Unsupported realtime event type"):
        await RedisRunRealtimePublisher(redis).publish(event)

    assert redis.published == []
