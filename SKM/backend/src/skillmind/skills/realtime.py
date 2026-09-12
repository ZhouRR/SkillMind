"""Skill interpretation の非永続進行 event を Redis Pub/Sub へ配送する。

正本は skill_interpretations の永続 record であり、本 channel は SSE 表示専用。
配送失敗で interpretation 本体を失敗させてはならない。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError

from skillmind.runs.realtime import RedisPublisher

# 進行 event の許可語彙。契約は contracts/events/skill-interpret-event/v1 と同期する。
INTERPRET_EVENT_NAMES = frozenset({
    "interpret.queued",
    "interpret.started",
    "interpret.prompt",
    "interpret.delta",
    "interpret.completed",
    "interpret.failed",
    "interpret.unknown",
    "interpret.disconnected",
})


def interpret_channel(execution_key: str) -> str:
    """Execution key ごとの Pub/Sub channel 名を返す。"""

    return f"skillmind:interpret:{execution_key}"


def interpret_event_data(
    *, event: str, execution_key: str, data: Mapping[str, Any]
) -> dict[str, Any]:
    """SSE と Pub/Sub が共有する secret-free な公開 event shape を構築する。"""

    if event not in INTERPRET_EVENT_NAMES:
        raise ValueError(f"Unsupported interpret event: {event}")
    return {
        "event": event,
        "execution_key": execution_key,
        "occurred_at": datetime.now(UTC).isoformat(),
        "data": dict(data),
    }


class RedisInterpretEventPublisher:
    """Interpretation 進行 event を execution key の channel へ配送する adapter。"""

    def __init__(self, redis: RedisPublisher) -> None:
        """Worker の Redis client を publish port として保持する。"""

        self._redis = redis

    async def publish(
        self, *, execution_key: str, event: str, data: Mapping[str, Any]
    ) -> None:
        """進行 event を配送する。Redis 障害は表示品質の劣化に留め、実行は続行する。"""

        message = interpret_event_data(event=event, execution_key=execution_key, data=data)
        try:
            await self._redis.publish(
                interpret_channel(execution_key),
                json.dumps(message, ensure_ascii=False, separators=(",", ":")),
            )
        except RedisError:
            return
