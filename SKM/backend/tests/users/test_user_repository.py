"""管理用の共通 Org→User→Session query 境界を PostgreSQL SQL と fake 結果で確認する。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.service import AuthenticatedActor
from skillmind.db.models import AuthSession, User
from skillmind.users.domain import UserAccess
from skillmind.users.repository import UserRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only_actor", [False, True])
async def test_common_lock_queries_gate_org_then_sorted_users_then_original_session(
    read_only_actor: bool,
) -> None:
    """target session を不要に巻き込まず、対象 User の状態を同じ transaction で固定する。"""

    actor_id, organization_id = uuid4(), uuid4()
    target_id = None if read_only_actor else uuid4()
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
    users.all.return_value = [actor] if read_only_actor else [actor, target]
    sessions.all.return_value = [current]
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=object())
    session.scalars = AsyncMock(side_effect=[users, sessions])

    repository = UserRepository(session)
    if read_only_actor:
        locked = await repository.lock_users(
            access=access, target_id=None, include_target_sessions=False, read_only_actor=True,
        )
    else:
        # 既存の呼出元が新引数を渡さなくても、管理 User の UPDATE lock を維持する。
        locked = await repository.lock_users(
            access=access, target_id=target_id, include_target_sessions=False,
        )

    dialect_factory: Callable[[], Dialect] = PGDialect
    queries = [call.args[0].compile(dialect=dialect_factory())
               for call in session.mock_calls if call[0] in {"scalar", "scalars"}]
    assert len(queries) == 3
    assert "FROM organizations" in str(queries[0])
    assert list(queries[0].params.values()) == [organization_id]
    assert "FROM users" in str(queries[1]) and "ORDER BY users.id" in str(queries[1])
    assert "users.organization_id =" in str(queries[1])
    expected_ids = [actor_id] if target_id is None else sorted([actor_id, target_id])
    assert expected_ids in queries[1].params.values()
    assert "users.status =" not in str(queries[1])
    assert "FROM auth_sessions" in str(queries[2])
    assert "ORDER BY auth_sessions.id" in str(queries[2])
    assert set(queries[2].params.values()) == {actor_id, credential.session_token_hash}
    assert all("FOR UPDATE" in str(query) for query in (queries[0], queries[2]))
    assert ("FOR SHARE" if read_only_actor else "FOR UPDATE") in str(queries[1])
    assert "FOR KEY SHARE" not in str(queries[1])
    assert all(
        call.args[0].get_execution_options()["populate_existing"] is True
        for call in session.scalars.call_args_list
    )
    assert locked.target is (None if read_only_actor else target)
    assert locked.actor is actor and locked.current_session is current


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "has_target,include_sessions", [(True, False), (False, True), (True, True)]
)
async def test_read_only_actor_lock_cannot_be_used_for_account_management(
    has_target: bool, include_sessions: bool,
) -> None:
    """共有 actor lock を対象変更/会話失効用へ拡張し、後から更新 lock に昇格させない。"""

    session = MagicMock(spec=AsyncSession)
    with pytest.raises(ValueError, match="Read-only actor locking"):
        await UserRepository(session).lock_users(
            access=MagicMock(spec=UserAccess),
            target_id=uuid4() if has_target else None,
            include_target_sessions=include_sessions,
            read_only_actor=True,
        )
    session.scalar.assert_not_called()
    session.scalars.assert_not_called()
