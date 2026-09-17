"""表示専用 Pub/Sub の待機を制限し、障害中に業務処理を繰り返し待たせない。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from time import monotonic
from typing import Protocol

from redis.exceptions import RedisError


class RedisPublisher(Protocol):
    """共有 Redis client の非永続 publish だけを使用する。"""

    async def publish(self, channel: str, message: str) -> int:
        """購読者への配送を試みる。永続保存や業務成功を意味しない。"""
        ...


class BestEffortPublisher:
    """表示通知だけに短い timeout と障害時の待避を適用する。Outbox には使わない。"""

    def __init__(
        self,
        redis: RedisPublisher,
        *,
        timeout_seconds: float = 0.25,
        cooldown_seconds: float = 2.0,
    ) -> None:
        """待機上限は内部設定とし、利用者に新しい入力や設定を要求しない。"""
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                timeout_seconds,
                cooldown_seconds,
            )
        ):
            raise ValueError("Notification intervals must be finite and positive")
        self._redis = redis
        self._timeout = timeout_seconds
        self._cooldown = cooldown_seconds
        self._resume_at = 0.0

    async def publish(self, channel: str, payload: Mapping[str, object]) -> None:
        """失敗した一時通知を再送せず、取消は原 caller へそのまま返す。"""
        if monotonic() < self._resume_at:
            return
        # 形式違反は transport 障害と混同しない。待避中は大きな prompt の JSON 化も省く。
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            async with asyncio.timeout(self._timeout):
                await self._redis.publish(channel, message)
        except (RedisError, OSError, TimeoutError):
            # 完成本文・終態は従来の DB/Outbox が正本。取消や業務例外は捕捉しない。
            self._resume_at = monotonic() + self._cooldown
