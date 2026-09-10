"""Schedule の内部参加者が現身分と原認領を同じ Run transaction へ束縛する。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skillmind.db.models import Project, ProjectMember, User
from skillmind.projects.domain import ProjectArchivedError
from skillmind.projects.repository import LockedProjectAccess, ProjectRepository
from skillmind.runs.creation_request import TaskRunIntent
from skillmind.runs.domain import CreatedRun, RunStatus
from skillmind.schedules.creation import ScheduleRunCreationParticipant
from skillmind.schedules.domain import (
    ScheduleClaimLostError,
    ScheduleOverlapError,
    ScheduleOwnerUnavailableError,
)
from skillmind.schedules.repository import ScheduleRepository
from tests.schedules.fakes import claimed_schedule


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ADMIN", "USER"])
async def test_creation_authority_uses_current_user_and_shared_project_guard(role: str) -> None:
    """古い tick actor を使わず、User→Project/Member→認領→原要求を順番に検査する。"""

    claim = claimed_schedule()
    session = MagicMock()
    user = User(id=claim.created_by, organization_id=uuid4(), system_role=role, status="ACTIVE")
    project = Project(id=claim.project_id, organization_id=user.organization_id, status="ACTIVE")
    member = ProjectMember(project_id=project.id, user_id=user.id, status="ACTIVE")
    access = LockedProjectAccess(user, project, member)
    intent = TaskRunIntent(
        claim.project_id,
        claim.skill_version_id,
        claim.task_key,
        claim.created_by,
        claim.input_json,
        claim.sources,
    )
    order = MagicMock()
    for name in ("creator", "project", "claim"):
        order.attach_mock(AsyncMock(), name)
    order.creator.return_value = user
    order.project.return_value = access
    with (
        patch.object(ScheduleRepository, "lock_creator", order.creator),
        patch.object(ProjectRepository, "lock_write_access", order.project),
        patch.object(ScheduleRepository, "lock_claim", order.claim),
        patch.object(ScheduleRepository, "validate_original_request") as original,
    ):
        authority = await ScheduleRunCreationParticipant(claim).authorize(
            session,
            intent=intent,
            idempotency_key="original-key",
        )
    assert [call[0] for call in order.mock_calls] == ["creator", "project", "claim"]
    original.assert_called_once_with(
        order.claim.return_value, intent=intent, idempotency_key="original-key"
    )
    assert authority.actor_system_role == role
    assert authority.project_membership == ("ADMIN_BYPASS" if role == "ADMIN" else "ACTIVE")


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["missing-owner", "disabled-owner", "archived-project"])
async def test_unavailable_owner_stops_before_reading_the_claim(denial: str) -> None:
    """発火権限が失効したときは原 Run 照会へ到達する前に止める。"""

    claim = claimed_schedule()
    user = User(
        id=claim.created_by,
        organization_id=uuid4(),
        system_role="USER",
        status="DISABLED" if denial == "disabled-owner" else "ACTIVE",
    )
    intent = TaskRunIntent(
        claim.project_id,
        claim.skill_version_id,
        claim.task_key,
        claim.created_by,
        claim.input_json,
        claim.sources,
    )
    with (
        patch.object(
            ScheduleRepository,
            "lock_creator",
            AsyncMock(return_value=None if denial == "missing-owner" else user),
        ),
        patch.object(
            ProjectRepository,
            "lock_write_access",
            AsyncMock(side_effect=ProjectArchivedError("Archived")),
        ),
        patch.object(ScheduleRepository, "lock_claim", AsyncMock()) as locked,
        pytest.raises(ScheduleOwnerUnavailableError),
    ):
        await ScheduleRunCreationParticipant(claim).authorize(
            MagicMock(), intent=intent, idempotency_key="original-key"
        )
    locked.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED", "ARCHIVED", "ERROR"])
async def test_an_already_claimed_occurrence_survives_schedule_control_changes(status: str) -> None:
    """暂停/归档は将来の認領だけを止め、原 PENDING の作成権を取り消さない。"""

    claim = claimed_schedule()
    locked = SimpleNamespace(
        schedule=SimpleNamespace(status=status), occurrence=SimpleNamespace(status="PENDING")
    )
    with (
        patch.object(ScheduleRepository, "lock_claim", AsyncMock(return_value=locked)) as fence,
        patch.object(
            ScheduleRepository, "has_overlapping_run", AsyncMock(return_value=False)
        ) as overlap,
    ):
        await ScheduleRunCreationParticipant(claim).before_create(MagicMock())
    overlap.assert_awaited_once_with(locked)
    assert fence.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["settled", "overlap", "expired-after-query"])
async def test_new_creation_stops_on_settlement_overlap_or_late_fence_loss(cause: str) -> None:
    """照会待機の後でも期限を確認し、結算済みを新しい Run に使い回さない。"""

    claim = claimed_schedule()
    locked = SimpleNamespace(
        occurrence=SimpleNamespace(status="SETTLED" if cause == "settled" else "PENDING")
    )
    fences = (
        [locked, ScheduleClaimLostError("expired")] if cause == "expired-after-query" else [locked]
    )
    with (
        patch.object(ScheduleRepository, "lock_claim", AsyncMock(side_effect=fences)),
        patch.object(
            ScheduleRepository, "has_overlapping_run", AsyncMock(return_value=cause == "overlap")
        ),
        pytest.raises(ScheduleOverlapError if cause == "overlap" else ScheduleClaimLostError),
    ):
        await ScheduleRunCreationParticipant(claim).before_create(MagicMock())


@pytest.mark.asyncio
async def test_creation_completion_uses_the_callers_session_and_original_occurrence() -> None:
    """Run service の session のまま元の occurrence へ関連付け、別 transaction を開かない。"""

    claim = claimed_schedule()
    run = CreatedRun(
        uuid4(), claim.project_id, uuid4(), RunStatus.QUEUED, 1, datetime.now(UTC), False
    )
    session = MagicMock()
    with patch("skillmind.schedules.creation.ScheduleRepository") as factory:
        factory.return_value.record_outcome = AsyncMock()
        await ScheduleRunCreationParticipant(claim).complete(session, run)
    factory.assert_called_once_with(session)
    recorded_claim, result = factory.return_value.record_outcome.call_args.args
    assert recorded_claim is claim
    assert result.schedule_id == claim.schedule_id
    assert result.occurrence_at == claim.occurrence_at
    assert result.run_id == run.run_id
