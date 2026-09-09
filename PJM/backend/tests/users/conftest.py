"""User 用例の実 domain/model と、DB transaction の呼出境界を組み合わせる。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from projectmind.auth.domain import generate_session_credentials, hash_password
from projectmind.auth.service import AuthenticatedActor
from projectmind.db.models import AuthSession, User
from projectmind.users.domain import UserAccess
from projectmind.users.repository import LockedUsers, UserRepository
from projectmind.users.service import UserService

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
PASSWORD = "test-only original password"


class Clock(datetime):
    """実 clock を待たず、lock/計算の前後で時刻が変わることを再現する。"""

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        """Fixture が指定した現在時刻を返す。"""

        return cls.current


@dataclass
class UserHarness:
    """実 password/model と fake query を分け、実 DB の証拠と誤認させない。"""

    service: UserService
    repository: Mock
    session: Mock
    transaction: AsyncMock
    factory: Mock
    actor: User
    target: User
    current: AuthSession
    other_sessions: tuple[AuthSession, ...]
    access: UserAccess
    added: list[object]
    order: list[str]


@pytest.fixture
def users(monkeypatch: pytest.MonkeyPatch) -> UserHarness:
    """SQL は mock、credential 判定・用例・監査の投影は実装を使う。"""

    Clock.current = NOW
    monkeypatch.setattr("projectmind.users.service.datetime", Clock)
    organization_id = uuid4()

    def user(role: str) -> User:
        """本番データを用いず、永続 default を明示した User を用意する。"""

        identity = uuid4()
        return User(
            id=identity,
            organization_id=organization_id,
            email=f"{identity}@example.test",
            display_name="Test account",
            password_hash=hash_password(PASSWORD),
            system_role=role,
            status="ACTIVE",
            row_version=1,
            created_at=NOW,
            updated_at=NOW,
        )

    actor, target = user("ADMIN"), user("USER")
    credentials = generate_session_credentials()

    def auth_session(owner: User) -> AuthSession:
        """現在 cookie と照合できる v2 model を明示して期限を固定する。"""

        return AuthSession(
            id=uuid4(),
            user_id=owner.id,
            token_hash=credentials.session_token_hash,
            csrf_token_hash=credentials.csrf_token_hash,
            credential_version=2,
            system_role_at_login=owner.system_role,
            idle_expires_at=NOW + timedelta(minutes=30),
            absolute_expires_at=NOW + timedelta(hours=8),
            revoked_at=None,
            created_at=NOW,
            last_seen_at=NOW,
        )

    current, others = auth_session(actor), (auth_session(target), auth_session(target))
    others[1].idle_expires_at = NOW - timedelta(minutes=1)
    access = UserAccess(
        AuthenticatedActor(
            actor.id, organization_id, actor.email, actor.display_name, actor.system_role
        ),
        uuid4(),
        credentials.session_token,
        credentials.csrf_token,
    )
    session = Mock()
    added: list[object] = []
    order: list[str] = []

    def add(item: object) -> None:
        """親子の INSERT 準備順を記録する。"""

        order.append(type(item).__name__)
        added.append(item)

    async def flush() -> None:
        """実 DB の flush を模倣せず、呼出し順だけを記録する。"""

        order.append("flush")

    session.add.side_effect = add
    session.flush = AsyncMock(side_effect=flush)
    transaction = AsyncMock()
    transaction.__aexit__.return_value = False
    session.begin.return_value = transaction
    context = AsyncMock()
    context.__aenter__.return_value = session
    context.__aexit__.return_value = False
    factory = Mock(return_value=context)
    repository = Mock(spec=UserRepository)
    real = UserRepository(session)
    repository.to_stored.side_effect = real.to_stored
    repository.append_event.side_effect = real.append_event
    repository.insert_user = AsyncMock(side_effect=real.insert_user)
    repository.email_exists = AsyncMock(return_value=False)
    repository.has_other_active_admin = AsyncMock(return_value=True)

    async def lock_users(
        *, access: UserAccess, target_id: object, include_target_sessions: bool
    ) -> LockedUsers:
        """Query 結果の差替えを可能にし、SQL の順序自体は repository test へ委ねる。"""

        selected = actor if target_id == actor.id else target if target_id == target.id else None
        return LockedUsers(actor, selected, current, (current, *others))

    repository.lock_users = AsyncMock(side_effect=lock_users)
    monkeypatch.setattr("projectmind.users.service.UserRepository", Mock(return_value=repository))
    return UserHarness(
        UserService(factory),
        repository,
        session,
        transaction,
        factory,
        actor,
        target,
        current,
        others,
        access,
        added,
        order,
    )
