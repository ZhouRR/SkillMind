"""User UI 言語 preference API の認証、CSRF、値域を検証する。"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from skillmind.auth.service import (
    AuthenticatedActor,
    CsrfRejectedError,
    UnauthorizedSessionError,
)


class UiLanguageAuthService:
    """UI 言語 route 用の session/CSRF と preference 保存 fake。"""

    def __init__(self) -> None:
        """固定 actor、token と未設定 preference を生成する。"""

        self.session_token = "session-token"
        self.csrf_token = "csrf-token"
        self.ui_language: str | None = None
        self.actor = AuthenticatedActor(
            user_id=uuid4(),
            organization_id=uuid4(),
            email="user@example.com",
            display_name="User",
            system_role="USER",
        )

    async def authenticate_session(self, session_token: str) -> AuthenticatedActor:
        """一致する opaque session だけを認証する。"""

        if session_token != self.session_token:
            raise UnauthorizedSessionError("hidden")
        return self.actor

    async def authenticate_unsafe_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
    ) -> AuthenticatedActor:
        """Session と CSRF が一致する mutation だけを認証する。"""

        actor = await self.authenticate_session(session_token)
        if csrf_token != self.csrf_token:
            raise CsrfRejectedError("hidden")
        return actor

    async def get_ui_language(self, *, actor: AuthenticatedActor) -> str | None:
        """Actor 自身の保存済み UI 言語を返す。"""

        del actor
        return self.ui_language

    async def set_ui_language(
        self,
        *,
        actor: AuthenticatedActor,
        ui_language: str | None,
    ) -> str | None:
        """許可集合検証済みの UI 言語を保存する。"""

        del actor
        self.ui_language = ui_language
        return ui_language


def test_user_can_read_and_update_own_ui_language(client: TestClient) -> None:
    """認証済み User が CSRF 付きで nullable UI 言語を更新できる。"""

    auth = UiLanguageAuthService()
    client.app.state.auth_service = auth
    client.cookies.set("skillmind_session", auth.session_token)

    initial = client.get("/api/v1/users/me/ui-language")
    updated = client.put(
        "/api/v1/users/me/ui-language",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"ui_language": "ja"},
    )
    cleared = client.put(
        "/api/v1/users/me/ui-language",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"ui_language": None},
    )

    assert initial.status_code == 200
    assert initial.json() == {"ui_language": None}
    assert updated.status_code == 200
    assert updated.json() == {"ui_language": "ja"}
    assert cleared.status_code == 200
    assert cleared.json() == {"ui_language": None}


def test_ui_language_rejects_values_outside_allowed_set(client: TestClient) -> None:
    """許可集合(zh/ja/en)以外の値は request validation で拒否される。"""

    auth = UiLanguageAuthService()
    client.app.state.auth_service = auth
    client.cookies.set("skillmind_session", auth.session_token)

    response = client.put(
        "/api/v1/users/me/ui-language",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"ui_language": "fr"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    # 拒否時は preference が変更されない。
    assert auth.ui_language is None


def test_ui_language_requires_session_and_valid_csrf(client: TestClient) -> None:
    """UI 言語 read は session、write はさらに有効な CSRF token を要求する。"""

    auth = UiLanguageAuthService()
    client.app.state.auth_service = auth

    unauthenticated = client.get("/api/v1/users/me/ui-language")
    client.cookies.set("skillmind_session", auth.session_token)
    rejected = client.put(
        "/api/v1/users/me/ui-language",
        headers={"Origin": "http://testserver", "X-CSRF-Token": "invalid"},
        json={"ui_language": "en"},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "authentication_required"
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "csrf_rejected"
