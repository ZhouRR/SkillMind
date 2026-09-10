"""ProjectService の ADMIN/USER authorization 境界を検証する。"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.auth.service import AuthenticatedActor
from skillmind.projects import ProjectPermissionDeniedError, ProjectService
from skillmind.projects.domain import ProjectDeleteBlockedError
from skillmind.projects.repository import ProjectRepository
from skillmind.users.domain import UserAccess
from tests.projects.project_harness import Members


def _user_actor() -> AuthenticatedActor:
    """Project 管理権限を持たない USER actor を生成する。"""

    return AuthenticatedActor(
        user_id=uuid4(),
        organization_id=uuid4(),
        email="user@example.com",
        display_name="User",
        system_role="USER",
    )


@pytest.mark.asyncio
async def test_user_cannot_create_or_manage_members_before_database_access() -> None:
    """USER の管理操作を database lookup より先に拒否する。"""

    service = ProjectService(cast(async_sessionmaker[AsyncSession], object()))
    actor = _user_actor()

    with pytest.raises(ProjectPermissionDeniedError):
        await service.create_project(
            access=UserAccess(actor, uuid4(), "", ""),
            key="denied",
            name="Denied",
            description="",
            settings={},
            retention_days=90,
        )
    with pytest.raises(ProjectPermissionDeniedError):
        await service.add_member(
            access=UserAccess(actor, uuid4(), "", ""), project_id=uuid4(), user_id=uuid4(),
        )
    with pytest.raises(ProjectPermissionDeniedError):
        await service.delete_project(
            access=UserAccess(actor, uuid4(), "", ""), project_id=uuid4(), expected_row_version=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_delete_keeps_repository_failure_inside_the_single_transaction_boundary(
    monkeypatch: pytest.MonkeyPatch, blocked: bool, members: Members,
) -> None:
    """参照拒否も DB 障害も同一 transaction の出口へ渡す。実 rollback の検証は別に行う。"""

    failure = (
        ProjectDeleteBlockedError("Schedule exists", blockers=("task_schedule_exists",))
        if blocked else RuntimeError("test database failure")
    )
    remove = AsyncMock(side_effect=failure)
    monkeypatch.setattr(ProjectRepository, "delete", remove)
    session, transaction = members.session, members.transaction
    session.scalar.side_effect = [members.project]

    with pytest.raises(type(failure)) as raised:
        await members.service.delete_project(
            access=members.access, project_id=members.project.id, expected_row_version=1,
        )

    assert raised.value is failure
    session.begin.assert_called_once_with()
    remove.assert_awaited_once_with(
        project=members.project, expected_row_version=1,
    )
    transaction.__aexit__.assert_awaited_once()
    assert transaction.__aexit__.call_args.args[:2] == (type(failure), failure)
