"""AuthService の Redis 短期防御を database なしで検証する。"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.login_protection import LoginProtectionUnavailableError, LoginRateLimitedError
from projectmind.auth.service import AuthService, InvalidCredentialsError, LoginCsrfError


class FakeRedis:
    """Login CSRF と rate limit に必要な Redis command だけを再現する。"""

    def __init__(self) -> None:
        """平文 credential を含まない key/value store を初期化する。"""

        self.values: dict[str, str | int] = {}

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        """TTL 引数を必須にして短期 challenge を保存する。"""

        assert ex == 300
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | int | None:
        """Challenge を原子的に一度だけ取得・削除する。"""

        return self.values.pop(key, None)

    async def eval(self, *args: object) -> list[int]:
        """本 test では admission を許可する。Lua の原子性は実 Redis test が検証する。"""

        return [1, 0]


def _service(redis: FakeRedis, *, timeout: float = 2) -> AuthService:
    """Database path に到達しない test 用 AuthService を構築する。"""

    return AuthService(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(Redis, redis),
        login_csrf_ttl_seconds=300,
        session_idle_minutes=30,
        session_absolute_hours=12,
        admin_session_absolute_hours=8,
        login_attempts_per_minute=5,
        login_account_attempts_per_minute=15,
        login_source_requests_per_minute=100,
        login_protection_timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_login_csrf_is_single_use_and_redis_key_hides_plaintext() -> None:
    """Login challenge が一回だけ消費され、Redis key に平文を含まないことを確認する。"""

    redis = FakeRedis()
    service = _service(redis)
    token = await service.issue_login_csrf(await service.begin_login("192.0.2.10"))

    assert all(token not in key for key in redis.values)
    with pytest.raises(InvalidCredentialsError):
        await service.login(
            email="invalid-email",
            password="untrusted password value",
            login_csrf_header=token,
            login_csrf_cookie=token,
            client_address="192.0.2.10",
            admission=await service.begin_login("192.0.2.10"),
        )
    with pytest.raises(LoginCsrfError):
        await service.login(
            email="invalid-email",
            password="untrusted password value",
            login_csrf_header=token,
            login_csrf_cookie=token,
            client_address="192.0.2.10",
            admission=await service.begin_login("192.0.2.10"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_account_rejection_stops_before_challenge_password_and_database(
    monkeypatch: pytest.MonkeyPatch,
    blocked: bool,
) -> None:
    """Account 配額の拒否・不明状態では challenge 消費も password/DB 呼出しも行わない。"""

    redis = FakeRedis()
    service = _service(redis)
    admission = await service.begin_login("192.0.2.10")
    redis.eval = AsyncMock(return_value=[0, 120] if blocked else ["invalid", 0])
    redis.getdel = AsyncMock()
    verify = Mock(side_effect=AssertionError("Password verification must not run"))
    monkeypatch.setattr("projectmind.auth.service.verify_password", verify)
    with pytest.raises(LoginRateLimitedError if blocked else LoginProtectionUnavailableError):
        await service.login(
            email="reader@example.com",
            password="test-only long passphrase",
            login_csrf_header="c" * 32,
            login_csrf_cookie="c" * 32,
            client_address="192.0.2.10",
            admission=admission,
        )
    redis.getdel.assert_not_awaited()
    verify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["set", "getdel"])
@pytest.mark.parametrize("failure", ["disconnect", "timeout"])
async def test_challenge_store_failure_never_reaches_password(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    failure: str,
) -> None:
    """Challenge の保存・消費が確認できない時、短い deadline で停止して再試行しない。"""

    redis = FakeRedis()
    service = _service(redis, timeout=0.01)

    async def unavailable(*args: object, **kwargs: object) -> None:
        """外部 store に接続せず、timeout または明示的な切断を作る。"""

        if failure == "disconnect":
            raise RedisConnectionError("test-only store failure")
        await asyncio.Event().wait()

    stub = AsyncMock(side_effect=unavailable)
    setattr(redis, command, stub)
    verify = Mock(side_effect=AssertionError("Password verification must not run"))
    monkeypatch.setattr("projectmind.auth.service.verify_password", verify)
    with pytest.raises(LoginProtectionUnavailableError):
        admission = await service.begin_login("192.0.2.10")
        if command == "set":
            await service.issue_login_csrf(admission)
        else:
            await service.login(
                email="reader@example.com",
                password="test-only long passphrase",
                login_csrf_header="c" * 32,
                login_csrf_cookie="c" * 32,
                client_address="192.0.2.10",
                admission=admission,
            )
    stub.assert_awaited_once()
    verify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["set", "getdel"])
async def test_unconfirmed_challenge_state_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """SET 確認欠落や GETDEL の破損 marker を発行/検証成功へ変換しない。"""

    redis = FakeRedis()
    service = _service(redis)
    setattr(redis, command, AsyncMock(return_value=False if command == "set" else "corrupt"))
    verify = Mock(side_effect=AssertionError("Password verification must not run"))
    monkeypatch.setattr("projectmind.auth.service.verify_password", verify)
    admission = await service.begin_login("192.0.2.10")
    with pytest.raises(LoginProtectionUnavailableError):
        if command == "set":
            await service.issue_login_csrf(admission)
        else:
            await service.login(
                email="reader@example.com",
                password="test-only long passphrase",
                login_csrf_header="c" * 32,
                login_csrf_cookie="c" * 32,
                client_address="192.0.2.10",
                admission=admission,
            )
    verify.assert_not_called()
