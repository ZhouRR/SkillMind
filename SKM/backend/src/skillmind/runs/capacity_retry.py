"""モデル容量不足だけを対象とする有限再試行の共通規則。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

MODEL_CAPACITY_CODE = "model_capacity_unavailable"
CAPACITY_MAX_ATTEMPTS = 3


def capacity_retry_delay(attempt_no: int, max_attempts: int) -> int | None:
    """Segment 内の初回を含め最大 3 回、既存の上限が小さければそれを優先する。"""

    if 1 <= attempt_no < min(max_attempts, CAPACITY_MAX_ATTEMPTS):
        return (15, 30)[attempt_no - 1]
    return None


def capacity_retry_at(error: Mapping[str, object] | None) -> datetime | None:
    """容量待ちの持久期限を読む。壊れた期限で早期再送しない。"""

    if not error or error.get("code") != MODEL_CAPACITY_CODE or error.get("retryable") is not True:
        return None
    value = error.get("retry_at")
    if not isinstance(value, str):
        raise ValueError("Capacity retry deadline is missing")
    deadline = datetime.fromisoformat(value)
    if deadline.tzinfo is None:
        raise ValueError("Capacity retry deadline must include timezone")
    return deadline
