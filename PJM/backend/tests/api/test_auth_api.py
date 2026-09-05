"""Login、session、logout の認証 API 契約を検証する。"""

from __future__ import annotations

from fakes import FakeAuthService
from fastapi.testclient import TestClient


def test_login_context_login_session_and_logout_use_secure_contract(client: TestClient) -> None:
    """Login CSRF、opaque cookie、session refresh、logout が同じ認証契約を共有する。"""

    service = FakeAuthService()
    client.app.state.auth_service = service

    context = client.get("/api/v1/auth/login-context")
    assert context.status_code == 200
    assert context.json()["csrf_token"] == service.login_csrf
    assert "HttpOnly" in context.headers["set-cookie"]
    assert "SameSite=strict" in context.headers["set-cookie"]

    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver", "X-CSRF-Token": service.login_csrf},
        json={"email": "admin@example.com", "password": "correct password value"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["system_role"] == "ADMIN"
    assert login.json()["csrf_token"] == service.csrf_token
    assert "projectmind_session=" in login.headers["set-cookie"]

    current = client.get("/api/v1/auth/session")
    assert current.status_code == 200
    assert current.json()["user"]["email"] == "admin@example.com"

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://testserver", "X-CSRF-Token": service.csrf_token},
    )
    assert logout.status_code == 204
    assert service.logged_out is True


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

    invalid = client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://testserver", "X-CSRF-Token": service.login_csrf},
        json={"email": "admin@example.com", "password": "wrong password value"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "Invalid email or password."
    assert "hidden" not in invalid.text


def test_current_session_returns_generic_unauthorized_problem(client: TestClient) -> None:
    """Session 不在と失効を区別せず同じ 401 Problem として公開する。"""

    client.app.state.auth_service = FakeAuthService(unauthorized=True)
    response = client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
