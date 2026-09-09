"""ProjectService の ADMIN/USER authorization 境界を検証する。"""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.service import AuthenticatedActor
from projectmind.projects import ProjectPermissionDeniedError, ProjectService
from projectmind.projects.domain import ProjectDeleteBlockedError
from projectmind.projects.repository import ProjectRepository


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
            actor=actor,
            key="denied",
            name="Denied",
            description="",
            settings={},
            retention_days=90,
        )
    with pytest.raises(ProjectPermissionDeniedError):
        await service.add_member(actor=actor, project_id=uuid4(), user_id=uuid4())
    with pytest.raises(ProjectPermissionDeniedError):
        await service.delete_project(actor=actor, project_id=uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_delete_keeps_repository_failure_inside_the_single_transaction_boundary(
    monkeypatch: pytest.MonkeyPatch, blocked: bool,
) -> None:
    """参照拒否も DB 障害も同一 transaction の出口へ渡す。実 rollback の検証は別に行う。"""

    failure = (
        ProjectDeleteBlockedError("Schedule exists", blockers=("task_schedule_exists",))
        if blocked else RuntimeError("test database failure")
    )
    remove = AsyncMock(side_effect=failure)
    monkeypatch.setattr(ProjectRepository, "delete", remove)
    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = session.begin.return_value
    transaction.__aexit__.return_value = False
    factory = MagicMock(return_value=session)
    actor = replace(_user_actor(), system_role="ADMIN")
    project_id = uuid4()

    with pytest.raises(type(failure)) as raised:
        await ProjectService(factory).delete_project(actor=actor, project_id=project_id)

    assert raised.value is failure
    factory.assert_called_once_with()
    session.begin.assert_called_once_with()
    remove.assert_awaited_once_with(
        organization_id=actor.organization_id, project_id=project_id,
    )
    transaction.__aexit__.assert_awaited_once()
    assert transaction.__aexit__.call_args.args[:2] == (type(failure), failure)
