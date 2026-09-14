"""API key header の優先順位、CSRF 分離、管理応答の秘密境界を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from skillmind.auth.api_key_domain import CreatedApiKey, StoredApiKey
from skillmind.auth.domain import derive_api_key_proof, generate_api_key_credentials
from skillmind.auth.sessions import UnauthorizedSessionError
from tests.api.fakes import FakeAuthService


@pytest.fixture
def keys(client: TestClient) -> AsyncMock:
    """Transport test の key service。実 DB の資格検証は別の回帰で扱う。"""
    auth = FakeAuthService()
    service = AsyncMock()
    service.authenticate.return_value = (auth.actor, uuid4())
    service.list.return_value = ()
    client.app.state.api_key_service = service
    return service


@pytest.mark.parametrize("header", ["Authorization", "X-API-Key"])
def test_key_authorizes_write_without_cookie_origin_or_browser_csrf(
    client: TestClient,
    keys: AsyncMock,
    header: str,
) -> None:
    """unsafe dependency と transaction 引渡しが原 key と内部証明を保持する。"""
    token = generate_api_key_credentials().session_token
    record = StoredApiKey(
        uuid4(), "Application", token[:13], uuid4(), datetime.now(UTC), None, None
    )
    keys.create.return_value = CreatedApiKey(record, token)
    client.headers.clear()
    client.cookies.clear()
    result = client.post(
        "/api/v1/api-keys",
        json={"name": "Application"},
        headers={header: f"Bearer {token}" if header == "Authorization" else token},
    )
    assert result.status_code == 201
    assert result.headers["cache-control"] == "no-store"
    access = keys.create.call_args.args[0]
    assert access.session_token == token
    assert access.csrf_token == derive_api_key_proof(token)
    assert token not in repr(access)
    assert result.json()["token"] == token
    listed = client.get(
        "/api/v1/api-keys",
        headers={header: f"Bearer {token}" if header == "Authorization" else token},
    )
    assert listed.status_code == 200 and token not in listed.text


@pytest.mark.parametrize(
    "headers",
    [
        [("Authorization", "Basic invalid")],
        [("Authorization", "")],
        [("X-API-Key", "")],
        [("Authorization", "Bearer invalid")],
        [("Authorization", "Bearer invalid"), ("X-API-Key", "invalid")],
        [("X-API-Key", "invalid"), ("X-API-Key", "invalid")],
        [("Authorization", "Bearer invalid"), ("Authorization", "Bearer invalid")],
    ],
)
def test_explicit_bad_or_ambiguous_key_never_falls_back_to_cookie(
    client: TestClient,
    keys: AsyncMock,
    headers: list[tuple[str, str]],
) -> None:
    """Cookie が有効でも header を無視して管理者として続行しない。"""
    keys.authenticate.side_effect = UnauthorizedSessionError("invalid")
    result = client.get("/api/v1/api-keys", headers=headers)
    assert result.status_code == 401
    keys.list.assert_not_called()


def test_cookie_write_still_requires_origin_and_csrf(client: TestClient, keys: AsyncMock) -> None:
    """新 header 方式は既存 cookie 認証の CSRF を弱めない。"""
    client.headers.pop("Origin")
    assert client.post("/api/v1/api-keys", json={"name": "x"}).status_code == 403
    client.headers["Origin"] = "http://testserver"
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v1/api-keys", json={"name": "x"}).status_code == 422
    keys.create.assert_not_called()


@pytest.mark.parametrize("name", ["", " ", "x" * 201])
def test_invalid_name_is_rejected_before_issuance(
    client: TestClient, keys: AsyncMock, name: str
) -> None:
    """空名や過大名によって識別不能な秘密を作らない。"""
    assert client.post("/api/v1/api-keys", json={"name": name}).status_code == 422
    keys.create.assert_not_called()
