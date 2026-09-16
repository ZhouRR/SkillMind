"""業務制御を変更せず、固定名の処理時間を本文なしで観測する。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from functools import wraps
from time import perf_counter
from typing import Literal, ParamSpec, TypeVar
from uuid import UUID

from skillmind.core.logging import log_event

P = ParamSpec("P")
R = TypeVar("R")
Phase = Literal["engine_wait", "event_persist", "realtime_publish", "result_finalize"]
_PHASES: tuple[Phase, ...] = (
    "engine_wait", "event_persist", "realtime_publish", "result_finalize",
)
logger = logging.getLogger(__name__)


def _correlation(args: tuple[object, ...], kwargs: dict[str, object]) -> dict[str, UUID]:
    """既知の UUID だけを抽出し、任意の引数・本文・例外文字列をログへ渡さない。"""

    result: dict[str, UUID] = {}
    for name in ("run_id", "run_attempt_id"):
        value = kwargs.get(name)
        if value is None and len(args) > 1:
            value = getattr(args[1], name, None)
        if isinstance(value, UUID):
            result[name] = value
    return result


def timed_async(event: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """既存 async 処理の戻り値・例外・取消を保持し、終了時に一件だけ計測する。"""

    def decorate(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        """呼出し signature と注釈を保持する decorator を構築する。"""

        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            """追加の await/Task/DB 操作を作らず、元 coroutine を一度だけ実行する。"""

            started = perf_counter()
            completed = False
            try:
                result = await function(*args, **kwargs)
                completed = True
                return result
            finally:
                # 観測の失敗で業務結果・元例外・CancelledError を置き換えない。
                try:
                    if logger.isEnabledFor(logging.INFO):
                        log_event(
                            logger, logging.INFO, event,
                            duration_ms=round((perf_counter() - started) * 1000, 3),
                            status="returned" if completed else "raised_or_cancelled",
                            **_correlation(args, kwargs),
                        )
                except Exception:
                    pass

        return wrapped

    return decorate


class ExecutionTimings:
    """一 Attempt 内の待機を集計し、token ごとのログ・永続イベントを増やさない。"""

    def __init__(self) -> None:
        """モデル本文や引数を保持せず、固定 phase の合計秒数と回数だけを用意する。"""

        self._seconds: dict[Phase, float] = dict.fromkeys(_PHASES, 0.0)
        self._counts: dict[Phase, int] = dict.fromkeys(_PHASES, 0)

    @contextmanager
    def measure(self, phase: Phase) -> Iterator[None]:
        """既存 await の周囲を測り、例外・取消でも元の制御フローを保持する。"""

        started = perf_counter()
        try:
            yield
        finally:
            self._seconds[phase] += perf_counter() - started
            self._counts[phase] += 1

    def emit(self, *, run_id: UUID, run_attempt_id: UUID) -> None:
        """Attempt 終了時に使われた phase のみ出力し、集計結果を業務条件にしない。"""

        try:
            if not logger.isEnabledFor(logging.INFO):
                return
            for phase in _PHASES:
                if self._counts[phase]:
                    log_event(
                        logger, logging.INFO, "run.performance." + phase,
                        run_id=run_id, run_attempt_id=run_attempt_id,
                        duration_ms=round(self._seconds[phase] * 1000, 3),
                        sample_count=self._counts[phase],
                    )
        except Exception:
            # ハンドラの I/O 失敗は結果 commit や stream cleanup の成否に影響させない。
            pass


def safe_observation(event: str, **fields: object) -> None:
    """固定分類・数値・ID のみを渡し、観測先の障害を業務へ伝播しない。"""
    with suppress(Exception):
        log_event(logger, logging.INFO, event, **fields)


@contextmanager
def observe_phase(event: str, **fields: object) -> Iterator[None]:
    """元の例外・取消を保持し、下位区間の経過だけを記録する。"""
    started = perf_counter()
    try:
        yield
    finally:
        safe_observation(event, duration_ms=round((perf_counter() - started) * 1000, 3), **fields)
