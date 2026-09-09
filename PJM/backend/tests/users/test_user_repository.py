"""管理用の共通 Org→User→Session query 境界を PostgreSQL SQL と fake 結果で確認する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import AuthSession, User
from projectmind.users.domain import UserAccess
from projectmind.users.repository import UserRepository


@pytest.mark.asyncio
async def test_common_lock_queries_gate_org_then_sorted_users_then_original_session() -> None:
    """target session を不要に巻き込まず、対象 User の状態を同じ transaction で固定する。"""

    actor_id, target_id, organization_id = uuid4(), uuid4(), uuid4()
    credential = generate_session_credentials()
    access = UserAccess(
        AuthenticatedActor(actor_id, organization_id, "admin@example.test", "Admin", "ADMIN"),
        uuid4(), credential.session_token, credential.csrf_token,
    )
    actor = User(id=actor_id, organization_id=organization_id)
    target = User(id=target_id, organization_id=organization_id, status="DISABLED")
    current = AuthSession(
        id=uuid4(), user_id=actor_id, token_hash=credential.session_token_hash,
        created_at=datetime.now(UTC),
    )
    users, sessions = MagicMock(), MagicMock()
    users.all.return_value = [actor, target]
    sessions.all.return_value = [current]
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=object())
    session.scalars = AsyncMock(side_effect=[users, sessions])

    locked = await UserRepository(session).lock_users(
        access=access, target_id=target_id, include_target_sessions=False,
    )

    queries = [call.args[0].compile(dialect=postgresql.dialect())
               for call in session.mock_calls if call[0] in {"scalar", "scalars"}]
    assert len(queries) == 3
    assert "FROM organizations" in str(queries[0])
    assert list(queries[0].params.values()) == [organization_id]
    assert "FROM users" in str(queries[1]) and "ORDER BY users.id" in str(queries[1])
    assert "users.organization_id =" in str(queries[1])
    assert sorted([actor_id, target_id]) in queries[1].params.values()
    assert "users.status =" not in str(queries[1])
    assert "FROM auth_sessions" in str(queries[2])
    assert "ORDER BY auth_sessions.id" in str(queries[2])
    assert set(queries[2].params.values()) == {actor_id, credential.session_token_hash}
    assert all("FOR UPDATE" in str(query) for query in queries)
    assert locked.target is target and locked.current_session is current
