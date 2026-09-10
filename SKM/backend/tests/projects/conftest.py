"""外部サービスを呼ばず、Project 管理用の原 credential と transaction を共有する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.service import AuthenticatedActor
from skillmind.db.models import AuthSession, Project, User
from skillmind.projects.service import ProjectService
from skillmind.users.domain import UserAccess
from skillmind.users.repository import LockedUsers, UserRepository
from tests.projects.project_harness import Members


@pytest.fixture
def members(monkeypatch: pytest.MonkeyPatch) -> Members:
    """外部サービスを使わず、原 cookie と照合可能な現在 ADMIN/対象 User を用意する。"""

    now = datetime.now(UTC)
    organization_id = uuid4()
    actor = User(
        id=uuid4(), organization_id=organization_id, email="admin@example.test",
        display_name="Admin", password_hash="unused", system_role="ADMIN", status="ACTIVE",
        row_version=1, created_at=now, updated_at=now,
    )
    target = User(
        id=uuid4(), organization_id=organization_id, email="member@example.test",
        display_name="Member", password_hash="unused", system_role="USER", status="ACTIVE",
        row_version=1, created_at=now, updated_at=now,
    )
    credentials = generate_session_credentials()
    current = AuthSession(
        id=uuid4(), user_id=actor.id, token_hash=credentials.session_token_hash,
        csrf_token_hash=credentials.csrf_token_hash, credential_version=2,
        system_role_at_login="ADMIN", idle_expires_at=now + timedelta(minutes=30),
        absolute_expires_at=now + timedelta(hours=8), revoked_at=None,
        created_at=now, last_seen_at=now,
    )
    access = UserAccess(
        AuthenticatedActor(actor.id, organization_id, actor.email, actor.display_name, "ADMIN"),
        uuid4(), credentials.session_token, credentials.csrf_token,
    )
    project = Project(
        id=uuid4(), organization_id=organization_id, key="members", name="Members",
        description="", status="ACTIVE", settings_json={}, retention_days=90,
        row_version=1, created_at=now, updated_at=now,
    )
    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = session.begin.return_value
    transaction.__aexit__.return_value = False
    locked = LockedUsers(actor, target, current, (current,))
    lock_users = AsyncMock(return_value=locked)
    monkeypatch.setattr(UserRepository, "lock_users", lock_users)
    return Members(
        ProjectService(MagicMock(return_value=session)), session, transaction,
        access, locked, project, lock_users,
    )
