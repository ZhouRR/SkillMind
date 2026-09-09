"""Body 検証前の來源 gate と、認証 route の拒否応答を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fakes import FakeAuthService
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.api.problems import PROBLEM_DETAILS_SCHEMA
from projectmind.auth.login_protection import LoginProtectionUnavailableError, LoginRateLimitedError


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/login",
        "/api/v1/auth/login/",
        "/projectmind/api/v1/auth/login",
        "/api/v1/auth/login-context",
        "/api/v1/auth/login-context/",
    ],
)
@pytest.mark.parametrize(
    "error", [LoginRateLimitedError(120), LoginProtectionUnavailableError("hidden")]
)
def test_source_rejection_precedes_json_origin_and_csrf(
    client: TestClient,
    path: str,
    error: Exception,
) -> None:
    """不正 JSON や不足 header より前に、全入口の同じ來源 quota を強制する。"""

    service = FakeAuthService()
    service.begin_login = AsyncMock(side_effect=error)
    service.issue_login_csrf = AsyncMock()
    service.login = AsyncMock()
    client.app.state.auth_service = service
    response = client.post(
        path,
        content="invalid-json",
        follow_redirects=False,
        headers={"Origin": "", "Content-Type": "application/json"},
    )
    assert response.status_code == (429 if isinstance(error, LoginRateLimitedError) else 503)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA, format_checker=FormatChecker()).validate(
        response.json()
    )
    assert "hidden" not in response.text
    if isinstance(error, LoginRateLimitedError):
        assert response.headers["retry-after"] == "120"
        assert response.json()["code"] == "login_rate_limited"
    else:
        assert response.json()["code"] == "login_protection_unavailable"
        assert "retry-after" not in response.headers
    service.begin_login.assert_awaited_once()
    service.issue_login_csrf.assert_not_awaited()
    service.login.assert_not_awaited()


@pytest.mark.parametrize(
    ("payload", "origin", "status"),
    [
        ({}, "http://testserver", 422),
        ({"email": "reader@example.com", "password": "password"}, "", 403),
    ],
)
def test_rejected_input_still_consumes_one_source_request(
    client: TestClient,
    payload: dict[str, str],
    origin: str,
    status: int,
) -> None:
    """Pydantic と Origin で終わる request も來源の保護範囲から落とさない。"""

    service = FakeAuthService()
    client.app.state.auth_service = service
    result = client.post("/api/v1/auth/login", json=payload, headers={"Origin": origin})
    assert result.status_code == status
    assert len(service.login_sources) == 1


@pytest.mark.parametrize(
    "error", [LoginRateLimitedError(60), LoginProtectionUnavailableError("hidden")]
)
def test_account_gate_uses_the_same_problem_factory(client: TestClient, error: Exception) -> None:
    """來源通過後の account 拒否も、同じ公開 code/header と no-store になる。"""

    service = FakeAuthService()
    service.login = AsyncMock(side_effect=error)
    client.app.state.auth_service = service
    result = client.post(
        "/api/v1/auth/login",
        json={
            "email": "reader@example.com",
            "password": "password",
        },
    )
    assert result.status_code == (429 if isinstance(error, LoginRateLimitedError) else 503)
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["content-type"] == "application/problem+json"
    assert result.json()["request_id"] == result.headers["x-request-id"]
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA, format_checker=FormatChecker()).validate(
        result.json()
    )
    if isinstance(error, LoginRateLimitedError):
        assert result.headers["retry-after"] == "60"
    else:
        assert "retry-after" not in result.headers


def test_arbitrary_forwarded_headers_do_not_change_admission_source(client: TestClient) -> None:
    """Application 自身が任意 X-Forwarded-For を來源 identity として信用しない。"""

    service = FakeAuthService()
    client.app.state.auth_service = service
    for address in ("192.0.2.1", "198.51.100.9"):
        assert (
            client.get(
                "/api/v1/auth/login-context",
                headers={
                    "X-Forwarded-For": address,
                    "Forwarded": f"for={address}",
                },
            ).status_code
            == 200
        )
    assert len(service.login_sources) == 2
    assert service.login_sources[0] == service.login_sources[1] == "testclient"


def test_source_protection_does_not_replace_session_authentication(client: TestClient) -> None:
    """Login の Redis 故障で、有効会話の read/logout まで無条件に止めない。"""

    service = FakeAuthService()
    service.begin_login = AsyncMock(side_effect=LoginProtectionUnavailableError("hidden"))
    client.app.state.auth_service = service
    client.cookies.set("projectmind_session", service.session_token)
    assert client.get("/api/v1/auth/session").status_code == 200
    assert (
        client.post(
            "/api/v1/auth/logout",
            headers={
                "X-CSRF-Token": service.csrf_token,
            },
        ).status_code
        == 204
    )
    service.begin_login.assert_not_awaited()
