"""会話読取・失効・lock 後の時刻を実 service と明示的な SQL fake で検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.domain import (
    SESSION_CREDENTIAL_VERSION,
    generate_session_credentials,
    hash_password,
)
from projectmind.auth.login_protection import LoginRateLimitedError
from projectmind.auth.service import AuthService, CsrfRejectedError, UnauthorizedSessionError
from projectmind.db.models import AuthSession, User

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class ScalarRow:
    """SELECT の一行だけを返し、実 DB の競合保証は模倣しない。"""

    def __init__(self, row: User | AuthSession | None) -> None:
        """この query の戻り値を固定する。"""

        self.row = row

    def one_or_none(self) -> User | AuthSession | None:
        """SQLAlchemy scalar result と同じ一行取得面を持つ。"""

        return self.row


class SessionDatabase:
    """一組の会話と user を共有し、query 順序・transaction 更新を観測する。"""

    def __init__(self) -> None:
        """外部 credential を用いず、新 v2 会話を test ごとに発行する。"""

        self.credentials = generate_session_credentials()
        self.user = User(
            id=uuid4(),
            organization_id=uuid4(),
            email="reader@example.com",
            display_name="Reader",
            system_role="USER",
            status="ACTIVE",
            password_hash=hash_password("test-only long passphrase"),
            last_login_at=None,
        )
        self.row: AuthSession | None = AuthSession(
            id=uuid4(),
            user_id=self.user.id,
            token_hash=self.credentials.session_token_hash,
            csrf_token_hash=self.credentials.csrf_token_hash,
            credential_version=SESSION_CREDENTIAL_VERSION,
            system_role_at_login="USER",
            created_at=NOW - timedelta(minutes=10),
            last_seen_at=NOW - timedelta(minutes=1),
            idle_expires_at=NOW + timedelta(minutes=29),
            absolute_expires_at=NOW + timedelta(hours=12),
            revoked_at=None,
        )
        self.statements: list[Select[Any]] = []
        self.after_query: Callable[[int], None] = lambda count: None
        self.added: list[AuthSession] = []
        self.transaction_lock = asyncio.Lock()

    def __call__(self) -> SessionDatabase:
        """既存 service の session factory 呼出しを受け取る。"""

        return self

    async def __aenter__(self) -> SessionDatabase:
        """Session scope の開始時に自分を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """外部 resource を作らないため閉じる対象はない。"""

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        """例外時に会話の可変値を戻す。実 PostgreSQL rollback の証明には使わない。"""

        async with self.transaction_lock:
            row = self.row
            before = (
                None if row is None else (row.last_seen_at, row.idle_expires_at, row.revoked_at)
            )
            try:
                yield
            except BaseException:
                if row is not None and before is not None:
                    row.last_seen_at, row.idle_expires_at, row.revoked_at = before
                raise

    async def scalars(self, statement: Select[Any]) -> ScalarRow:
        """対象 model を確認し、await 後に clock/row を変える hook を呼ぶ。"""

        self.statements.append(statement)
        self.after_query(len(self.statements))
        entity = statement.column_descriptions[0]["entity"]
        assert entity in {User, AuthSession}
        return ScalarRow(self.user if entity is User else self.row)

    def add(self, row: AuthSession) -> None:
        """新規 login の保存候補を記録する。"""

        self.added.append(row)


def service_for(
    database: SessionDatabase,
    *,
    idle: int = 30,
    absolute: int = 12,
    redis: Redis | None = None,
) -> AuthService:
    """会話検証を通る実 service と、login 成功に必要な Redis 応答を用意する。"""

    # redis-py の一部 command は def -> Awaitable のため、spec の自動判別へ任せない。
    redis = (
        redis
        if redis is not None
        else cast(
            Redis,
            SimpleNamespace(
                getdel=AsyncMock(return_value="1"),
                eval=AsyncMock(return_value=[1, 0]),
            ),
        )
    )
    return AuthService(
        cast(async_sessionmaker[AsyncSession], database),
        redis,
        login_csrf_ttl_seconds=300,
        session_idle_minutes=idle,
        session_absolute_hours=absolute,
        admin_session_absolute_hours=8,
        login_attempts_per_minute=5,
        login_account_attempts_per_minute=15,
        login_source_requests_per_minute=100,
        login_protection_timeout_seconds=2,
    )


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> SessionDatabase:
    """実時計や DB に依存せず、lock 後判定の時点を固定する。"""

    monkeypatch.setattr("projectmind.auth.service.datetime", SimpleNamespace(now=lambda zone: NOW))
    return SessionDatabase()


@pytest.mark.asyncio
async def test_two_pages_and_two_api_instances_keep_the_same_csrf(
    database: SessionDatabase,
) -> None:
    """別 service instance からの読取と逆順利用で既存 token を壊さない。"""

    first, second = service_for(database), service_for(database)
    token = database.credentials.session_token
    page_a, page_b = await asyncio.gather(first.get_session(token), second.get_session(token))
    assert page_a.csrf_token == page_b.csrf_token == database.credentials.csrf_token
    for service, page in ((second, page_b), (first, page_a)):
        actor = await service.authenticate_unsafe_session(
            session_token=token, csrf_token=page.csrf_token
        )
        assert actor.user_id == database.user.id
    assert database.row is not None
    assert database.row.csrf_token_hash == database.credentials.csrf_token_hash
    await first.get_session(token)
    await second.authenticate_unsafe_session(session_token=token, csrf_token=page_a.csrf_token)


@pytest.mark.asyncio
async def test_session_queries_lock_user_before_session(database: SessionDatabase) -> None:
    """後続の user 管理と共通の lock 順序を SQL 生成で固定する。"""

    await service_for(database).get_session(database.credentials.session_token)
    statements = [str(item.compile(dialect=postgresql.dialect())) for item in database.statements]
    assert len(statements) == 2
    assert "FOR UPDATE OF users" in statements[0]
    assert "JOIN auth_sessions" in statements[0]
    assert "FROM auth_sessions" in statements[1] and "FOR UPDATE" in statements[1]
    assert "auth_sessions.user_id =" in statements[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_number", [1, 2])
async def test_expiry_crossed_during_either_lock_is_not_extended(
    database: SessionDatabase,
    monkeypatch: pytest.MonkeyPatch,
    lock_number: int,
) -> None:
    """親/子どちらの待機中に期限を越えても、古い now で会話を延長しない。"""

    assert database.row is not None
    row = database.row
    row.last_seen_at = NOW - timedelta(minutes=10)
    row.idle_expires_at = NOW + timedelta(seconds=1)
    clock = SimpleNamespace(value=NOW)

    def complete_lock(count: int) -> None:
        """指定 lock を取得した時点で現在時刻が期限を越えたことを再現する。"""

        if count == lock_number:
            clock.value = NOW + timedelta(seconds=2)

    database.after_query = complete_lock
    monkeypatch.setattr(
        "projectmind.auth.service.datetime", SimpleNamespace(now=lambda zone: clock.value)
    )
    with pytest.raises(UnauthorizedSessionError):
        await service_for(database).authenticate_session(database.credentials.session_token)
    assert row.idle_expires_at == NOW + timedelta(seconds=1)
    assert row.last_seen_at == NOW - timedelta(minutes=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["revoked", "idle", "absolute", "disabled", "role", "version", "csrf_hash", "missing"]
)
async def test_session_rejection_applies_to_reads_and_writes(
    database: SessionDatabase, reason: str
) -> None:
    """旧 credential・撤権・破損を unsafe だけでなく read でも拒否する。"""

    assert database.row is not None
    if reason == "revoked":
        database.row.revoked_at = NOW
    elif reason == "idle":
        database.row.idle_expires_at = NOW
    elif reason == "absolute":
        database.row.absolute_expires_at = NOW
    elif reason == "disabled":
        database.user.status = "DISABLED"
    elif reason == "role":
        database.user.system_role = "ADMIN"
    elif reason == "version":
        database.row.credential_version = 1
    elif reason == "csrf_hash":
        database.row.csrf_token_hash = generate_session_credentials().csrf_token_hash
    else:
        database.row = None
    service = service_for(database)
    with pytest.raises(UnauthorizedSessionError):
        await service.get_session(database.credentials.session_token)
    with pytest.raises(UnauthorizedSessionError):
        await service.authenticate_session(database.credentials.session_token)
    with pytest.raises(UnauthorizedSessionError):
        await service.authenticate_unsafe_session(
            session_token=database.credentials.session_token,
            csrf_token=database.credentials.csrf_token,
        )


@pytest.mark.asyncio
async def test_legacy_tokens_are_rejected_before_database_lookup(database: SessionDatabase) -> None:
    """旧 token を v2 の hash や CSRF に読み替える fallback を設けない。"""

    with pytest.raises(UnauthorizedSessionError):
        await service_for(database).get_session("legacy-session-value")
    assert database.statements == []


@pytest.mark.asyncio
async def test_wrong_csrf_cannot_logout_or_extend_idle(database: SessionDatabase) -> None:
    """別会話の token で書込/登出できず、更新は transaction の rollback 対象となる。"""

    assert database.row is not None
    database.row.last_seen_at = NOW - timedelta(minutes=10)
    service = service_for(database)
    for operation in (service.authenticate_unsafe_session, service.logout):
        with pytest.raises(CsrfRejectedError):
            await operation(
                session_token=database.credentials.session_token,
                csrf_token=generate_session_credentials().csrf_token,
            )
    assert database.row.revoked_at is None
    assert database.row.last_seen_at == NOW - timedelta(minutes=10)


@pytest.mark.asyncio
async def test_logout_invalidates_both_read_and_write(database: SessionDatabase) -> None:
    """片方のページが登出した後、以前の CSRF と cookie を組み合わせても再利用できない。"""

    service = service_for(database)
    await service.logout(
        session_token=database.credentials.session_token, csrf_token=database.credentials.csrf_token
    )
    assert database.row is not None and database.row.revoked_at == NOW
    with pytest.raises(UnauthorizedSessionError):
        await service.get_session(database.credentials.session_token)
    with pytest.raises(UnauthorizedSessionError):
        await service.authenticate_unsafe_session(
            session_token=database.credentials.session_token,
            csrf_token=database.credentials.csrf_token,
        )


@pytest.mark.asyncio
async def test_login_freezes_protocol_role_and_caps_initial_idle(database: SessionDatabase) -> None:
    """User lock 後に新しい認証版を発行し、初期 idle が absolute を越えない。"""

    service = service_for(database, idle=240, absolute=1)
    result = await service.login(
        email=database.user.email,
        password="test-only long passphrase",
        login_csrf_header="one-use-challenge",
        login_csrf_cookie="one-use-challenge",
        client_address="192.0.2.10",
        admission=await service.begin_login("192.0.2.10"),
    )
    row = database.added[0]
    assert row.credential_version == 2 and row.system_role_at_login == "USER"
    assert row.idle_expires_at == row.absolute_expires_at == NOW + timedelta(hours=1)
    assert result.session_token.startswith("pm2.") and result.csrf_token.startswith("csrf2.")
    assert "FOR UPDATE" in str(database.statements[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_successful_logins_do_not_reset_real_redis_quota(
    database: SessionDatabase,
    isolated_redis: Redis,
) -> None:
    """実 Lua と実 password path を通しても、成功が共有配額を消さないことを守る。"""

    service = service_for(database, redis=isolated_redis)
    for _ in range(5):
        challenge = await service.issue_login_csrf(await service.begin_login("192.0.2.10"))
        await service.login(
            email=database.user.email,
            password="test-only long passphrase",
            login_csrf_header=challenge,
            login_csrf_cookie=challenge,
            client_address="192.0.2.10",
            admission=await service.begin_login("192.0.2.10"),
        )
    assert len(database.added) == 5
    assert len({row.token_hash for row in database.added}) == 5
    challenge = await service.issue_login_csrf(await service.begin_login("192.0.2.10"))
    with pytest.raises(LoginRateLimitedError):
        await service.login(
            email=database.user.email,
            password="test-only long passphrase",
            login_csrf_header=challenge,
            login_csrf_cookie=challenge,
            client_address="192.0.2.10",
            admission=await service.begin_login("192.0.2.10"),
        )
    assert len(database.added) == 5
    # Session の保存は fake であり、ここでは実 PostgreSQL の transaction を保証しない。
    assert len(await isolated_redis.keys("projectmind:auth:limit:v2:*")) == 3
