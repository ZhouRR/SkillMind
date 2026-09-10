"""Login、session、logout の認証 API 契約を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.auth.service import CsrfRejectedError, UnauthorizedSessionError
from tests.api.fakes import FakeAuthService


def assert_declared_problem(client: TestClient, response: Response) -> None:
    """実 response が同じ operation の media type、body、必須 header を満たすか確認する。"""

    operation = client.app.openapi()["paths"][response.request.url.path][
        response.request.method.lower()
    ]
    declared = operation["responses"][str(response.status_code)]
    assert response.headers["content-type"] == "application/problem+json"
    schema = declared["content"]["application/problem+json"]["schema"]
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response.json())
    assert response.json()["status"] == response.status_code
    assert response.json()["request_id"] == response.headers["x-request-id"]
    for name, header in declared["headers"].items():
        assert header["required"] is True
        Draft202012Validator(header["schema"]).validate(response.headers[name])


def test_login_context_login_session_and_logout_use_secure_contract(client: TestClient) -> None:
    """Login CSRF、opaque cookie、session refresh、logout が同じ認証契約を共有する。"""

    service = FakeAuthService()
    client.app.state.auth_service = service

    context = client.get("/api/v1/auth/login-context")
    assert context.status_code == 200
    assert context.json()["csrf_token"] == service.login_csrf
    assert "HttpOnly" in context.headers["set-cookie"]
    assert "SameSite=strict" in context.headers["set-cookie"]
    assert context.headers["cache-control"] == "no-store"

    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver", "X-CSRF-Token": service.login_csrf},
        json={"email": "admin@example.com", "password": "correct password value"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["system_role"] == "ADMIN"
    assert login.json()["csrf_token"] == service.csrf_token
    assert "skillmind_session=" in login.headers["set-cookie"]
    assert login.headers["cache-control"] == "no-store"

    current = client.get("/api/v1/auth/session")
    assert current.status_code == 200
    assert current.json()["user"]["email"] == "admin@example.com"
    assert current.headers["cache-control"] == "no-store"

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://testserver", "X-CSRF-Token": service.csrf_token},
    )
    assert logout.status_code == 204
    assert service.logged_out is True
    assert logout.headers["cache-control"] == "no-store"


def test_login_rejects_missing_origin_and_hides_credential_failure(client: TestClient) -> None:
    """Unsafe login の Origin 欠落を拒否し、credential failure の内部理由を隠す。"""

    service = FakeAuthService(invalid=True)
    client.app.state.auth_service = service
    client.get("/api/v1/auth/login-context")

    missing_origin = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "", "X-CSRF-Token": service.login_csrf},
        json={"email": "admin@example.com", "password": "wrong password value"},
    )
    assert missing_origin.status_code == 403
    assert missing_origin.json()["code"] == "csrf_rejected"
    assert missing_origin.headers["cache-control"] == "no-store"
    assert_declared_problem(client, missing_origin)

    invalid = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver", "X-CSRF-Token": service.login_csrf},
        json={"email": "admin@example.com", "password": "wrong password value"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "Invalid email or password."
    assert "hidden" not in invalid.text
    assert invalid.headers["cache-control"] == "no-store"
    assert_declared_problem(client, invalid)


def test_current_session_returns_generic_unauthorized_problem(client: TestClient) -> None:
    """Session 不在と失効を区別せず同じ 401 Problem として公開する。"""

    client.app.state.auth_service = FakeAuthService(unauthorized=True)
    response = client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"
    assert_declared_problem(client, response)


@pytest.mark.parametrize("payload", [{}, {"email": "invalid"}])
def test_invalid_login_requests_are_not_cacheable(
    client: TestClient, payload: dict[str, str]
) -> None:
    """Endpoint 本体の前に失敗する validation response も保存させない。"""

    response = client.post("/api/v1/auth/login", json=payload)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert_declared_problem(client, response)


@pytest.mark.parametrize(
    ("error", "status"),
    [(UnauthorizedSessionError("hidden"), 401), (CsrfRejectedError("hidden"), 403)],
)
def test_logout_rejections_match_the_openapi_problem(
    client: TestClient, error: Exception, status: int
) -> None:
    """Session 失効と CSRF 拒否を同じ公開 Problem 契約で返す。"""

    service = FakeAuthService()
    service.logout = AsyncMock(side_effect=error)
    client.app.state.auth_service = service
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == status
    assert "hidden" not in response.text
    assert_declared_problem(client, response)


def test_logout_missing_csrf_header_matches_problem_validation(client: TestClient) -> None:
    """Header の検証が route 実行前に失敗しても、既定 FastAPI JSON を返さない。"""

    del client.headers["X-CSRF-Token"]
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 422
    assert_declared_problem(client, response)
