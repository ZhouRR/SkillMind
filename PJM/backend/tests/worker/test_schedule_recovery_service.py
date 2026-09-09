"""Schedule tick の原認領回復・未知結果と普通 Run 結算の境界を検証する。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from projectmind.schedules.domain import (
    ScheduleClaimLostError,
    ScheduleOutcome,
    ScheduleTriggerResult,
)
from projectmind.schedules.repository import ScheduleRepository
from projectmind.schedules.service import ScheduleService
from tests.runs.test_task_run_service import _Session
from tests.schedules.fakes import claimed_schedule


def _service() -> ScheduleService:
    """外部接続のない session seam と未使用依存を注入する。"""

    return ScheduleService(_Session, skill_service=MagicMock(), run_service=MagicMock())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tick_recovers_original_pending_before_claiming_new_due_occurrences() -> None:
    """限られた tick 枠を原要求の回収に優先使用し、同じ予定を再構築しない。"""

    claim = claimed_schedule()
    service = _service()
    result = ScheduleTriggerResult(
        claim.schedule_id, claim.occurrence_at, ScheduleOutcome.RUN_CREATED, uuid4()
    )
    with (
        patch.object(
            ScheduleRepository, "claim_recoverable", AsyncMock(return_value=[claim])
        ) as recover,
        patch.object(ScheduleRepository, "list_due", AsyncMock()) as due,
        patch.object(service, "_try_fire", AsyncMock(return_value=result)) as fire,
    ):
        report = await service.run_due_schedules(limit=1)
    recover.assert_awaited_once()
    fire.assert_awaited_once_with(claim)
    due.assert_not_awaited()
    assert report.results == (result,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [OSError("transport unavailable"), TimeoutError(), ScheduleClaimLostError("expired")]
)
async def test_unconfirmed_execution_keeps_pending_instead_of_settling_failure(
    error: Exception,
) -> None:
    """基盤失敗/旧 lease を業務失効に変換せず、数値や原要求を変更しない。"""

    service = _service()
    with (
        patch.object(service, "_fire", AsyncMock(side_effect=error)),
        patch.object(ScheduleRepository, "record_outcome", AsyncMock()) as settle,
    ):
        assert await service._try_fire(claimed_schedule()) is None
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_does_not_become_an_outcome_or_continue_the_tick() -> None:
    """裸の job cancellation は伝播し、FAILED_PRECONDITION を補造しない。"""

    service = _service()
    with (
        patch.object(service, "_fire", AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await service._try_fire(claimed_schedule())


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [True, False])
async def test_only_known_no_run_outcomes_require_a_separate_settlement(created: bool) -> None:
    """Run 関連の TX C は廃止し、見送りだけを原認領に一回結算する。"""

    service = _service()
    claim = claimed_schedule()
    result = ScheduleTriggerResult(
        claim.schedule_id,
        claim.occurrence_at,
        ScheduleOutcome.RUN_CREATED if created else ScheduleOutcome.SKIPPED_OVERLAP,
        uuid4() if created else None,
    )
    with (
        patch.object(service, "_create_scheduled_run", AsyncMock(return_value=result)),
        patch.object(
            ScheduleRepository, "record_outcome", AsyncMock(return_value=result)
        ) as settle,
    ):
        assert await service._fire(claim) == result
    assert settle.await_count == (0 if created else 1)
    if not created:
        settle.assert_awaited_once_with(claim, result)
