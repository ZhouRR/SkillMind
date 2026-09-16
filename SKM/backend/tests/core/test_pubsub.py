"""通知 timeout・短期待避と、業務取消を隠さない共有送信を検証する。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from skillmind.core import pubsub
from skillmind.core.pubsub import BestEffortPublisher


async def test_publish_preserves_public_payload() -> None:
    """既存の channel と JSON shape を変えず一度だけ送る。"""
    redis = AsyncMock()
    sender = BestEffortPublisher(redis)
    await sender.publish("fixture", {"text": "日本語", "sequence": 7})
    redis.publish.assert_awaited_once()
    channel, message = redis.publish.await_args.args
    assert channel == "fixture"
    assert json.loads(message) == {"text": "日本語", "sequence": 7}


@pytest.mark.parametrize("error", [RedisConnectionError("fixture"), OSError("fixture")])
async def test_failed_notifications_are_not_retried_during_cooldown(monkeypatch, error) -> None:
    """100 個の delta が来ても障害先を100回待たず、復旧時は新しい通知だけを送る。"""
    now = [10.0]
    monkeypatch.setattr(pubsub, "monotonic", lambda: now[0])
    redis = AsyncMock()
    redis.publish.side_effect = error
    sender = BestEffortPublisher(redis, cooldown_seconds=2)
    await sender.publish("fixture", {"sequence": 1})
    for index in range(100):
        await sender.publish("fixture", {"sequence": index + 2})
    assert redis.publish.await_count == 1
    now[0] += 3
    redis.publish.side_effect = None
    await sender.publish("fixture", {"sequence": 200})
    assert redis.publish.await_count == 2
    assert json.loads(redis.publish.await_args.args[1]) == {"sequence": 200}


async def test_stalled_publish_is_bounded_and_cleans_up() -> None:
    """Redis が返らなくても送信 await を終了し、余分な background task を残さない。"""
    stopped = asyncio.Event()

    async def blocked(*_args):
        """応答しない transport を、取消時の終了事実とともに再現する。"""
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    redis = AsyncMock()
    redis.publish.side_effect = blocked
    sender = BestEffortPublisher(redis, timeout_seconds=0.01)
    await asyncio.wait_for(sender.publish("fixture", {"text": "a"}), timeout=1)
    assert stopped.is_set()
    await sender.publish("fixture", {"text": "b"})
    assert redis.publish.await_count == 1


async def test_caller_cancellation_is_not_swallowed() -> None:
    """Run の取消は通知品質の問題として握り潰さない。"""
    started = asyncio.Event()

    async def blocked(*_args):
        """送信開始後の親 task 取消を検証する。"""
        started.set()
        await asyncio.Event().wait()

    redis = AsyncMock()
    redis.publish.side_effect = blocked
    sender = BestEffortPublisher(redis)
    task = asyncio.create_task(sender.publish("fixture", {"text": "a"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sender._resume_at == 0


async def test_payload_programming_errors_remain_visible() -> None:
    """不正 payload を Redis の一時障害と偽らない。"""
    redis = AsyncMock()
    with pytest.raises(TypeError):
        await BestEffortPublisher(redis).publish("fixture", {"bad": object()})
    redis.publish.assert_not_awaited()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_internal_intervals_are_rejected(value) -> None:
    """内部 timeout を無期限または即時無効な値にしない。"""
    with pytest.raises(ValueError):
        BestEffortPublisher(AsyncMock(), timeout_seconds=value)
