"""調度の原 occurrence 照会が、権限と他 Run の重複検査を正しく分離することを確認する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from uuid import uuid4

import pytest

from projectmind.auth.service import AuthenticatedActor
from projectmind.runs.domain import CreatedRun, IdempotencyConflictError, RunStatus
from projectmind.runs.service import RunService
from projectmind.schedules.creation import ScheduleRunCreationParticipant
from projectmind.schedules.domain import ClaimedSchedule, ScheduleOutcome, schedule_idempotency_key
from projectmind.schedules.service import ScheduleService
from projectmind.skills import PublishedTaskNotFoundError
from projectmind.skills.service import SkillService
from tests.runs.test_task_run_service import _resolved
from tests.schedules.fakes import claimed_schedule


def _claimed() -> ClaimedSchedule:
    """外部 system を参照しない、認領済みの精確 task 設定を返す。"""

    return claimed_schedule()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
async def test_replay_checks_current_owner_before_ignoring_self_overlap(authorized: bool) -> None:
    """作成済みの自分の Run は重複扱いせず、失効 actor はその前で止める。"""

    claimed = _claimed()
    actor = AuthenticatedActor(
        user_id=claimed.created_by,
        organization_id=uuid4(),
        email="test@example.invalid",
        display_name="Test",
        system_role="USER",
    )
    runs = create_autospec(RunService, instance=True)
    replay = CreatedRun(
        run_id=uuid4(),
        project_id=claimed.project_id,
        task_id=uuid4(),
        status=RunStatus.RUNNING,
        row_version=3,
        created_at=datetime.now(UTC),
        idempotent_replay=True,
    )
    runs.find_task_run_replay.return_value = replay
    skills = MagicMock(spec=SkillService)
    service = ScheduleService(MagicMock(), skill_service=skills, run_service=runs)
    with (
        patch.object(
            service, "_authorize_creator", new=AsyncMock(return_value=actor if authorized else None)
        ),
        patch.object(
            service,
            "_overlapping_run",
            new=AsyncMock(side_effect=AssertionError("must not query overlap")),
        ) as overlap,
    ):
        result = await service._create_scheduled_run(claimed)
    overlap.assert_not_awaited()
    skills.resolve_task_run.assert_not_called()
    runs.create_task_run.assert_not_called()
    if authorized:
        assert result.outcome is ScheduleOutcome.RUN_CREATED
        assert result.run_id == replay.run_id
        assert runs.find_task_run_replay.call_args.kwargs[
            "idempotency_key"
        ] == schedule_idempotency_key(
            schedule_id=claimed.schedule_id, occurrence_at=claimed.occurrence_at
        )
        authorization = runs.find_task_run_replay.call_args.kwargs["authorization"]
        assert isinstance(authorization, ScheduleRunCreationParticipant)
        assert authorization._claim is claimed
    else:
        assert result.outcome is ScheduleOutcome.FAILED_PRECONDITION
        assert result.run_id is None
        runs.find_task_run_replay.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["task-disabled", "self-overlap", "other-overlap"])
async def test_schedule_rechecks_an_occurrence_committed_after_initial_lookup(race: str) -> None:
    """同時 commit が task 解決や重複照会に先行しても、自分の既存 Run へ収斂させる。"""

    claimed = _claimed()
    actor = AuthenticatedActor(
        user_id=claimed.created_by,
        organization_id=uuid4(),
        email="test@example.invalid",
        display_name="Test",
        system_role="USER",
    )
    replay = CreatedRun(
        run_id=uuid4(),
        project_id=claimed.project_id,
        task_id=uuid4(),
        status=RunStatus.RUNNING,
        row_version=3,
        created_at=datetime.now(UTC),
        idempotent_replay=True,
    )
    runs = create_autospec(RunService, instance=True)
    runs.find_task_run_replay.side_effect = [None, None if race == "other-overlap" else replay]
    skills = MagicMock(spec=SkillService)
    skills.resolve_task_run = AsyncMock(
        side_effect=PublishedTaskNotFoundError("Version is disabled")
    )
    service = ScheduleService(MagicMock(), skill_service=skills, run_service=runs)
    with (
        patch.object(service, "_authorize_creator", new=AsyncMock(return_value=actor)),
        patch.object(
            service,
            "_overlapping_run",
            new=AsyncMock(return_value=None if race == "task-disabled" else "RUNNING"),
        ),
    ):
        result = await service._create_scheduled_run(claimed)
    assert result.outcome is (
        ScheduleOutcome.SKIPPED_OVERLAP if race == "other-overlap" else ScheduleOutcome.RUN_CREATED
    )
    assert result.run_id == (None if race == "other-overlap" else replay.run_id)
    assert runs.find_task_run_replay.await_count == 2
    runs.create_task_run.assert_not_called()
    assert (
        runs.find_task_run_replay.await_args_list[0] == runs.find_task_run_replay.await_args_list[1]
    )


@pytest.mark.asyncio
async def test_ambiguous_original_schedule_request_is_a_precondition_failure() -> None:
    """旧要求を証明できない場合に、新しい key や資源で代用しない。"""

    claimed = _claimed()
    actor = AuthenticatedActor(
        user_id=claimed.created_by,
        organization_id=uuid4(),
        email="test@example.invalid",
        display_name="Test",
        system_role="USER",
    )
    runs = create_autospec(RunService, instance=True)
    runs.find_task_run_replay.side_effect = IdempotencyConflictError(
        "Original request is ambiguous"
    )
    service = ScheduleService(
        MagicMock(), skill_service=MagicMock(spec=SkillService), run_service=runs
    )
    with patch.object(service, "_authorize_creator", new=AsyncMock(return_value=actor)):
        result = await service._create_scheduled_run(claimed)
    assert result.outcome is ScheduleOutcome.FAILED_PRECONDITION
    assert result.run_id is None
    runs.create_task_run.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_lookup_and_create_share_the_original_internal_authorization() -> None:
    """照会 miss 後も同じ認領を通用作成へ渡し、人間の会話や caller role を補造しない。"""

    claimed = _claimed()
    actor = AuthenticatedActor(
        user_id=claimed.created_by,
        organization_id=uuid4(),
        email="worker@example.invalid",
        display_name="Worker owner",
        system_role="USER",
    )
    resolved = replace(
        _resolved(), skill_version_id=claimed.skill_version_id, task_key=claimed.task_key
    )
    runs = create_autospec(RunService, instance=True)
    runs.find_task_run_replay.return_value = None
    created = CreatedRun(
        run_id=uuid4(),
        project_id=claimed.project_id,
        task_id=uuid4(),
        status=RunStatus.QUEUED,
        row_version=1,
        created_at=datetime.now(UTC),
        idempotent_replay=False,
    )
    runs.create_task_run.return_value = created
    skills = create_autospec(SkillService, instance=True)
    skills.resolve_task_run.return_value = resolved
    session_factory = MagicMock()
    service = ScheduleService(session_factory, skill_service=skills, run_service=runs)
    with (
        patch.object(service, "_authorize_creator", AsyncMock(return_value=actor)),
        patch.object(service, "_overlapping_run", AsyncMock(return_value=None)),
    ):
        result = await service._create_scheduled_run(claimed)

    assert result.outcome is ScheduleOutcome.RUN_CREATED and result.run_id == created.run_id
    skills.resolve_task_run.assert_awaited_once_with(
        project_id=claimed.project_id,
        skill_version_id=claimed.skill_version_id,
        task_key=claimed.task_key,
        input_json=claimed.input_json,
    )
    runs.find_task_run_replay.assert_awaited_once()
    authorization = runs.find_task_run_replay.call_args.kwargs["authorization"]
    assert isinstance(authorization, ScheduleRunCreationParticipant)
    assert authorization._claim is claimed
    runs.create_task_run.assert_awaited_once_with(
        project_id=claimed.project_id,
        resolved=resolved,
        input_json=claimed.input_json,
        sources=claimed.sources,
        idempotency_key=schedule_idempotency_key(
            schedule_id=claimed.schedule_id, occurrence_at=claimed.occurrence_at
        ),
        trace_id=None,
        actor_id=claimed.created_by,
        authorization=authorization,
    )
    session_factory.assert_not_called()
