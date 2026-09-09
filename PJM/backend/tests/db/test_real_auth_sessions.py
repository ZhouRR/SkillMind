"""専用 PostgreSQL の会話読取、rollback、実 lock 待ちを検証する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from projectmind.auth.domain import SessionCredentials, generate_session_credentials, hash_password
from projectmind.auth.service import (
    AuthService,
    CsrfRejectedError,
    SessionResult,
    UnauthorizedSessionError,
)
from projectmind.db.models import AuthSession, User
from tests.db.test_real_database_invariants import (
    _insert_in_order,
    _organization,
    _session_factory,
)
from tests.db.test_real_database_invariants import (
    migrated_database_url as migrated_database_url,
)


def session_service(factory: async_sessionmaker[AsyncSession]) -> AuthService:
    """Redis を利用しない会話処理だけを、実 DB session factory へ接続する。"""

    return AuthService(
        factory,
        cast(Redis, object()),
        login_csrf_ttl_seconds=300,
        session_idle_minutes=30,
        session_absolute_hours=12,
        admin_session_absolute_hours=8,
        login_attempts_per_minute=5,
        login_account_attempts_per_minute=15,
        login_source_requests_per_minute=100,
        login_protection_timeout_seconds=2,
    )


async def seed_auth(
    factory: async_sessionmaker[AsyncSession],
    *,
    lifetime: timedelta = timedelta(minutes=30),
) -> tuple[User, AuthSession, SessionCredentials]:
    """実 FK 順を守り、test ごとに別 Organization/user/会話を作る。"""

    organization, credentials, now = (
        _organization(),
        generate_session_credentials(),
        datetime.now(UTC),
    )
    user = User(
        id=uuid4(),
        organization_id=organization.id,
        email=f"auth-{uuid4().hex}@example.com",
        password_hash=hash_password("test-only long passphrase"),
        display_name="Auth test",
        system_role="USER",
        status="ACTIVE",
    )
    row = AuthSession(
        id=uuid4(),
        user_id=user.id,
        token_hash=credentials.session_token_hash,
        csrf_token_hash=credentials.csrf_token_hash,
        credential_version=2,
        system_role_at_login="USER",
        created_at=now - timedelta(minutes=10),
        last_seen_at=now - timedelta(minutes=10),
        idle_expires_at=now + lifetime,
        absolute_expires_at=now + timedelta(hours=12),
        revoked_at=None,
    )
    async with factory() as session, session.begin():
        await _insert_in_order(session, organization, user, row)
    return user, row, credentials


async def test_postgres_two_api_instances_keep_the_session_csrf(migrated_database_url: str) -> None:
    """別 connection の並行読取後も、どちらの値でも同じ会話へ安全に書ける。"""

    async with _session_factory(migrated_database_url) as factory:
        user, row, credentials = await seed_auth(factory)
        first, second = session_service(factory), session_service(factory)
        pages = await asyncio.gather(
            first.get_session(credentials.session_token),
            second.get_session(credentials.session_token),
        )
        for page, service in ((pages[1], first), (pages[0], second)):
            assert page.csrf_token == credentials.csrf_token
            actor = await service.authenticate_unsafe_session(
                session_token=credentials.session_token, csrf_token=page.csrf_token
            )
            assert actor.user_id == user.id
        async with factory() as session:
            stored = await session.get(AuthSession, row.id)
            assert stored is not None and stored.csrf_token_hash == credentials.csrf_token_hash


async def test_postgres_wrong_csrf_rolls_back_idle_extension(migrated_database_url: str) -> None:
    """User/Session を読み延長した後の CSRF 拒否でも DB の値が変わらない。"""

    async with _session_factory(migrated_database_url) as factory:
        _, row, credentials = await seed_auth(factory)
        with pytest.raises(CsrfRejectedError):
            await session_service(factory).authenticate_unsafe_session(
                session_token=credentials.session_token,
                csrf_token=generate_session_credentials().csrf_token,
            )
        async with factory() as session:
            stored = await session.get(AuthSession, row.id)
            assert stored is not None
            assert stored.last_seen_at == row.last_seen_at
            assert stored.idle_expires_at == row.idle_expires_at


async def test_postgres_changed_role_cannot_upgrade_an_old_cookie(
    migrated_database_url: str,
) -> None:
    """現在 user の role だけを変更しても、旧 cookie から ADMIN actor を作らない。"""

    async with _session_factory(migrated_database_url) as factory:
        user, _, credentials = await seed_auth(factory)
        async with factory() as session, session.begin():
            locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
            assert locked is not None
            locked.system_role = "ADMIN"
        with pytest.raises(UnauthorizedSessionError):
            await session_service(factory).authenticate_session(credentials.session_token)


@pytest.mark.parametrize("lock_model", [User, AuthSession])
async def test_postgres_expiry_is_rechecked_after_each_row_lock(
    migrated_database_url: str,
    lock_model: type[User] | type[AuthSession],
) -> None:
    """pg_blocking_pids で実際の待機を確認してから期限を越え、延長されないことを検証する。"""

    async with _session_factory(migrated_database_url) as factory:
        user, row, credentials = await seed_auth(factory, lifetime=timedelta(seconds=5))
        label = f"pjm-auth-wait-{uuid4().hex}"
        engine = create_async_engine(
            migrated_database_url, connect_args={"server_settings": {"application_name": label}}
        )
        reader_factory = async_sessionmaker(engine, expire_on_commit=False)
        task: asyncio.Task[SessionResult] | None = None
        try:
            async with factory() as holder, holder.begin(), factory() as observer:
                holder_pid = await holder.scalar(select(func.pg_backend_pid()))
                target = user.id if lock_model is User else row.id
                await holder.scalar(
                    select(lock_model).where(lock_model.id == target).with_for_update()
                )
                task = asyncio.create_task(
                    session_service(reader_factory).get_session(credentials.session_token)
                )
                async with asyncio.timeout(5):
                    while not await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE application_name = :label "
                            "AND :holder = ANY(pg_blocking_pids(pid)))"
                        ),
                        {"label": label, "holder": holder_pid},
                    ):
                        await asyncio.sleep(0.01)
                assert datetime.now(UTC) < row.idle_expires_at
                # 実 DB の lock 保持を意図的に延ばし、取得前の時間では誤受理する条件を作る。
                await asyncio.sleep(
                    (row.idle_expires_at - datetime.now(UTC)).total_seconds() + 0.05
                )
            with pytest.raises(UnauthorizedSessionError):
                await asyncio.wait_for(task, 5)
            async with factory() as session:
                stored = await session.get(AuthSession, row.id)
                assert stored is not None and stored.idle_expires_at == row.idle_expires_at
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await engine.dispose()
