"""実 ASGI/Service/Redis を繋ぎ、password/DB 前の拒否境界を検証する。"""

from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from projectmind.api.main import create_app
from projectmind.auth.service import AuthService
from projectmind.core.hashing import sha256_hex
from projectmind.core.settings import Settings


def application(redis: Redis, *, source: int = 100, account: int = 15) -> FastAPI:
    """外部 lifespan を動かさず、専用 Redis と呼出し禁止 DB factory を注入する。"""

    app = create_app()
    app.state.settings = Settings(_env_file=None, environment="test", auth_cookie_secure=False)
    app.state.auth_service = AuthService(
        Mock(side_effect=AssertionError("Database access must not occur")),
        redis,
        login_csrf_ttl_seconds=300,
        session_idle_minutes=30,
        session_absolute_hours=12,
        admin_session_absolute_hours=8,
        login_attempts_per_minute=5,
        login_account_attempts_per_minute=account,
        login_source_requests_per_minute=source,
        login_protection_timeout_seconds=2,
    )
    return app


@pytest.mark.asyncio
async def test_malformed_http_requests_are_limited_by_real_redis(
    isolated_redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正 JSON の繰返しでも、実 HTTP 入口が password より前に来源を制限する。"""

    verify = Mock(side_effect=AssertionError("Password verification must not run"))
    monkeypatch.setattr("projectmind.auth.service.verify_password", verify)
    app = application(isolated_redis, source=3)
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(3):
            assert (await client.post("/api/v1/auth/login", content="bad-json")).status_code == 422
        response = await client.post("/api/v1/auth/login", content="bad-json")
        assert response.status_code == 429
        assert response.json()["code"] == "login_rate_limited"
        assert response.headers["retry-after"] == "60"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert (await client.get("/api/v1/auth/login-context")).status_code == 429
    verify.assert_not_called()
    assert len(await isolated_redis.keys("*")) == 1


@pytest.mark.asyncio
async def test_different_http_sources_share_account_quota_before_challenge(
    isolated_redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有効形式の email は IP を変えても合算し、challenge 拒否も計上する。"""

    verify = Mock(side_effect=AssertionError("Password verification must not run"))
    monkeypatch.setattr("projectmind.auth.service.verify_password", verify)
    app = application(isolated_redis, account=3)
    for index in range(4):
        transport = httpx.ASGITransport(app=app, client=(f"192.0.2.{index + 1}", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/auth/login",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": "c" * 32,
                },
                json={"email": "reader@example.com", "password": "test-only long passphrase"},
            )
            assert response.status_code == (403 if index < 3 else 429)
    verify.assert_not_called()


@pytest.mark.asyncio
async def test_corrupt_real_state_returns_unavailable_without_challenge(
    isolated_redis: Redis,
) -> None:
    """Store 破損を実 API の 503 に畳み込み、新たな challenge を作らない。"""

    key = f"projectmind:auth:limit:v2:source:{sha256_hex('192.0.2.10')}"
    await isolated_redis.set(key, "corrupt", ex=300)
    app = application(isolated_redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 12345)),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/auth/login-context")
    assert response.status_code == 503
    assert response.json()["code"] == "login_protection_unavailable"
    assert "set-cookie" not in response.headers
    assert "retry-after" not in response.headers
    assert await isolated_redis.get(key) == "corrupt"
