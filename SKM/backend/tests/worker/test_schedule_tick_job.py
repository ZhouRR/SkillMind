"""到期 schedule を発火する cron tick の登録と集計を検証する (計画 §22)。

tick が cron_jobs に載っていなければ、schedule は保存できるのに永遠に発火しない——UI 上は
「次回 03:00」と出たまま何も起きないため、気付くまでが長い。登録そのものをテストで固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from skillmind.schedules import ScheduleOutcome, ScheduleTickReport, ScheduleTriggerResult
from skillmind.worker.settings import WorkerSettings, trigger_due_schedules


class MemoryScheduleService:
    """発火結果を固定で返し、受信した batch 上限を確認する test service。"""

    def __init__(self) -> None:
        """受信した limit を記録する。"""

        self.limits: list[int] = []

    async def run_due_schedules(self, *, limit: int) -> ScheduleTickReport:
        """Worker settings 由来の batch 上限で三種の結末を返す。"""

        self.limits.append(limit)
        moment = datetime.now(UTC)
        return ScheduleTickReport(
            results=(
                ScheduleTriggerResult(
                    schedule_id=uuid4(),
                    occurrence_at=moment,
                    outcome=ScheduleOutcome.RUN_CREATED,
                    run_id=uuid4(),
                ),
                ScheduleTriggerResult(
                    schedule_id=uuid4(),
                    occurrence_at=moment,
                    outcome=ScheduleOutcome.SKIPPED_OVERLAP,
                ),
                ScheduleTriggerResult(
                    schedule_id=uuid4(),
                    occurrence_at=moment,
                    outcome=ScheduleOutcome.FAILED_PRECONDITION,
                ),
            )
        )


@pytest.mark.asyncio
async def test_schedule_tick_reports_each_outcome_separately() -> None:
    """作成・見送り・前提失効を混ぜず別々に集計する。

    三つを一つの数字へ潰すと「走っていない」のが正常な見送りなのか設定失効なのか読めない。
    """

    service = MemoryScheduleService()

    result = await trigger_due_schedules(
        {
            "schedule_service": service,
            "settings": type("S", (), {"outbox_batch_size": 25})(),
            "worker_id": "worker-1",
        }
    )

    assert result == {"status": "ok", "created": 1, "skipped": 1, "failed": 1}
    assert service.limits == [25]


def test_schedule_tick_is_registered_as_a_cron_job() -> None:
    """tick が Worker の cron_jobs に載っていることを確認する。"""

    names = {job.name for job in WorkerSettings.cron_jobs}

    assert "cron:trigger_due_schedules" in names
