"""観測は結果・例外・取消を変えず、本文を保存しないことを検証する。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from uuid import uuid4

import pytest

from skillmind.core import timing


@pytest.mark.asyncio
async def test_result_and_signature_survive_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    """元 coroutine は一回だけ呼ばれ、値と公開 signature を保持する。"""

    calls = []
    records = []
    monkeypatch.setattr(timing.logger, "isEnabledFor", lambda _: True)
    monkeypatch.setattr(timing, "log_event", lambda *args, **kwargs: records.append(kwargs))

    async def original(*, run_id, secret: str) -> object:
        """計測に公開してはいけない入力も受ける合成処理。"""
        calls.append(secret)
        return result

    result = object()
    run_id = uuid4()
    wrapped = timing.timed_async("run.performance.test")(original)
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert await wrapped(run_id=run_id, secret="private-input") is result
    assert calls == ["private-input"]
    assert len(records) == 1
    assert records[0]["run_id"] == run_id
    assert "private-input" not in str(records)
    assert "secret" not in records[0]
    assert records[0]["status"] == "returned"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("private-error"), asyncio.CancelledError()])
async def test_original_exception_survives_broken_logging(monkeypatch, failure) -> None:
    """ログ handler の障害で元の例外 instance や取消を置き換えない。"""

    def broken(*args, **kwargs):
        """ログ I/O 障害を再現する。"""
        raise RuntimeError("logging failed")

    monkeypatch.setattr(timing.logger, "isEnabledFor", lambda _: True)
    monkeypatch.setattr(timing, "log_event", broken)

    @timing.timed_async("run.performance.test")
    async def call():
        """既存業務例外をそのまま送出する。"""
        raise failure

    with pytest.raises(type(failure)) as caught:
        await call()
    assert caught.value is failure


@pytest.mark.asyncio
async def test_success_survives_broken_logging(monkeypatch) -> None:
    """完了した業務を観測障害だけで失敗に戻さない。"""

    def broken(*args, **kwargs):
        """失敗する handler。"""
        raise OSError("sink unavailable")

    monkeypatch.setattr(timing.logger, "isEnabledFor", lambda _: True)
    monkeypatch.setattr(timing, "log_event", broken)

    @timing.timed_async("run.performance.test")
    async def call():
        """外部 I/O を持たない処理。"""
        return 42

    assert await call() == 42


def test_timings_aggregate_and_do_not_log_per_event(monkeypatch) -> None:
    """多数の進捗 event を計測しても終了時の phase 一件だけに集約する。"""

    records = []
    ticks = iter(range(202))
    monkeypatch.setattr(timing, "perf_counter", lambda: float(next(ticks)))
    monkeypatch.setattr(timing.logger, "isEnabledFor", lambda _: True)
    monkeypatch.setattr(timing, "log_event", lambda *args, **kwargs: records.append((args, kwargs)))
    metrics = timing.ExecutionTimings()
    for _ in range(100):
        with metrics.measure("engine_wait"):
            pass
    assert records == []
    with pytest.raises(ValueError), metrics.measure("event_persist"):
        raise ValueError("do not disclose")
    metrics.emit(run_id=uuid4(), run_attempt_id=uuid4())
    assert len(records) == 2
    assert records[0][1]["sample_count"] == 100
    assert records[0][1]["duration_ms"] == 100_000
    assert records[1][1]["sample_count"] == 1
    assert "do not disclose" not in str(records)


def test_timing_fields_pass_the_production_log_allowlist(caplog) -> None:
    """追加件数が既存の scalar allowlist を通り、本文 field を増やさない。"""

    with caplog.at_level(logging.INFO, logger=timing.__name__):
        metrics = timing.ExecutionTimings()
        with metrics.measure("engine_wait"):
            pass
        metrics.emit(run_id=uuid4(), run_attempt_id=uuid4())
    assert len(caplog.records) == 1
    context = caplog.records[0].skillmind_context
    assert context["sample_count"] == 1
    assert set(context) == {"run_id", "run_attempt_id", "duration_ms", "sample_count"}


def test_emit_failure_does_not_escape(monkeypatch) -> None:
    """終了時の観測失敗も cleanup/結果を阻害しない。"""

    monkeypatch.setattr(timing.logger, "isEnabledFor", lambda _: True)

    def broken(*args, **kwargs):
        """出力先故障。"""
        raise OSError()

    monkeypatch.setattr(timing, "log_event", broken)
    metrics = timing.ExecutionTimings()
    with metrics.measure("result_finalize"):
        pass
    metrics.emit(run_id=uuid4(), run_attempt_id=uuid4())
