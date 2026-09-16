"""Run の非永続 realtime event を Redis Pub/Sub へ配送する。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.core.pubsub import BestEffortPublisher
from skillmind.core.pubsub import RedisPublisher as RedisPublisher


class RunRealtimePublisher(Protocol):
    """Worker から非永続 Run event を通知する port。"""

    async def publish(self, event: AgentEvent) -> None:
        """監査 DB へ保存しない realtime event を publish する。"""

        ...


class RedisRunRealtimePublisher:
    """TEXT_DELTA を Run ごとの Redis channel へ配送する adapter。"""

    def __init__(self, redis: RedisPublisher) -> None:
        """ARQ と通常 Redis client が共有する publish port を保持する。"""

        self._publisher = BestEffortPublisher(redis)

    async def publish(self, event: AgentEvent) -> None:
        """許可した realtime event だけを secret-free public shape で配送する。"""

        if event.event_type is not AgentEventType.TEXT_DELTA:
            raise ValueError(f"Unsupported realtime event type: {event.event_type}")
        text = event.payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("TEXT_DELTA must contain non-empty text")
        await self._publisher.publish(
            run_realtime_channel(event.run_id), realtime_event_data(event)
        )


def run_realtime_channel(run_id: UUID) -> str:
    """他 Run の delta を購読しない Run-scoped channel 名を返す。"""

    return f"skillmind:run-realtime:{run_id}"


def realtime_event_data(event: AgentEvent) -> dict[str, object]:
    """AgentEvent を RunEvent v1 と同じ browser 公開 shape へ変換する。"""

    return {
        "run_id": str(event.run_id),
        "run_attempt_id": str(event.run_attempt_id),
        "agent_session_id": event.agent_session_id,
        "sequence": event.sequence,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": dict(event.payload),
        "trace_id": None,
    }
