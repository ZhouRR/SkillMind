"""ProjectService の ADMIN/USER authorization 境界を検証する。"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.service import AuthenticatedActor
from projectmind.projects import ProjectPermissionDeniedError, ProjectService


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
