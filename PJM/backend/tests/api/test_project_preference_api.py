"""User Project preference API の認証、CSRF、resource hiding を検証する。"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from projectmind.auth.service import (
    AuthenticatedActor,
    CsrfRejectedError,
    UnauthorizedSessionError,
)
from projectmind.projects import ProjectNotFoundError, StoredProjectPreference


class PreferenceAuthService:
    """Preference route 用の session/CSRF fake。"""

    def __init__(self) -> None:
        """固定 actor と token を生成する。"""

        self.session_token = "session-token"
        self.csrf_token = "csrf-token"
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


class PreferenceProjectService:
    """Preference API の DTO 変換を検証する in-memory fake。"""

    def __init__(self, project_id: UUID | None = None) -> None:
        """初期 preference を保持する。"""

        self.project_id = project_id
        self.denied_project_id: UUID | None = None

    async def get_preference(self, *, actor: AuthenticatedActor) -> StoredProjectPreference:
        """Actor 自身の保存済み preference を返す。"""

        del actor
        return StoredProjectPreference(project_id=self.project_id)

    async def set_preference(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID | None,
    ) -> StoredProjectPreference:
        """拒否対象以外の preference を保存する。"""

        del actor
        if project_id == self.denied_project_id:
            raise ProjectNotFoundError("hidden")
        self.project_id = project_id
        return StoredProjectPreference(project_id=project_id)


def test_user_can_read_and_update_own_project_preference(client: TestClient) -> None:
    """認証済み User が CSRF 付きで nullable preference を更新できる。"""

    auth = PreferenceAuthService()
    project_id = uuid4()
    service = PreferenceProjectService()
    client.app.state.auth_service = auth
    client.app.state.project_service = service
    client.cookies.set("projectmind_session", auth.session_token)

    initial = client.get("/api/v1/users/me/project-preference")
    updated = client.put(
        "/api/v1/users/me/project-preference",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"project_id": str(project_id)},
    )

    assert initial.status_code == 200
    assert initial.json() == {"project_id": None}
    assert updated.status_code == 200
    assert updated.json() == {"project_id": str(project_id)}


def test_inaccessible_project_preference_is_hidden_as_not_found(client: TestClient) -> None:
    """無所属 Project の preference 更新を存在有無共通の 404 へ畳み込む。"""

    auth = PreferenceAuthService()
    denied_project_id = uuid4()
    service = PreferenceProjectService()
    service.denied_project_id = denied_project_id
    client.app.state.auth_service = auth
    client.app.state.project_service = service
    client.cookies.set("projectmind_session", auth.session_token)

    response = client.put(
        "/api/v1/users/me/project-preference",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"project_id": str(denied_project_id)},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "project_not_found"


def test_project_preference_requires_session_and_valid_csrf(client: TestClient) -> None:
    """Preference read は session、write はさらに有効な CSRF token を要求する。"""

    auth = PreferenceAuthService()
    client.app.state.auth_service = auth
    client.app.state.project_service = PreferenceProjectService()

    unauthenticated = client.get("/api/v1/users/me/project-preference")
    client.cookies.set("projectmind_session", auth.session_token)
    rejected = client.put(
        "/api/v1/users/me/project-preference",
        headers={"Origin": "http://testserver", "X-CSRF-Token": "invalid"},
        json={"project_id": None},
    )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "authentication_required"
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "csrf_rejected"
