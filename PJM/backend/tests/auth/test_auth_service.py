"""AuthService の Redis 短期防御を database なしで検証する。"""

from __future__ import annotations

from typing import cast

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    async def incr(self, key: str) -> int:
        """Login attempt counter を増加させる。"""

        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        """Rate counter に一分の有効期限が指定されることを確認する。"""

        assert key in self.values
        assert seconds == 60
        return True

    async def delete(self, key: str) -> int:
        """成功時 counter cleanup と同じ delete を提供する。"""

        return int(self.values.pop(key, None) is not None)


def _service(redis: FakeRedis) -> AuthService:
    """Database path に到達しない test 用 AuthService を構築する。"""

    return AuthService(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(Redis, redis),
        login_csrf_ttl_seconds=300,
        session_idle_minutes=30,
        session_absolute_hours=12,
        admin_session_absolute_hours=8,
        login_attempts_per_minute=5,
    )


@pytest.mark.asyncio
async def test_login_csrf_is_single_use_and_redis_key_hides_plaintext() -> None:
    """Login challenge が一回だけ消費され、Redis key に平文を含まないことを確認する。"""

    redis = FakeRedis()
    service = _service(redis)
    token = await service.issue_login_csrf()

    assert all(token not in key for key in redis.values)
    with pytest.raises(InvalidCredentialsError):
        await service.login(
            email="invalid-email",
            password="untrusted password value",
            login_csrf_header=token,
            login_csrf_cookie=token,
            client_address="192.0.2.10",
        )
    with pytest.raises(LoginCsrfError):
        await service.login(
            email="invalid-email",
            password="untrusted password value",
            login_csrf_header=token,
            login_csrf_cookie=token,
            client_address="192.0.2.10",
        )
