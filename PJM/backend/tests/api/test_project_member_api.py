"""所属 API の原 credential、キャッシュ禁止、公開形状と安定拒否を検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fakes import FakeAuthService
from fastapi.testclient import TestClient

from projectmind.api.problems import PROBLEM_DETAILS_SCHEMA
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.projects.domain import (
    ProjectMemberNotFoundError,
    ProjectMemberStatus,
    ProjectMemberUserNotFoundError,
    ProjectNotFoundError,
    StoredProjectMember,
)
from projectmind.projects.service import ProjectService
from projectmind.users.domain import UserAccess, UserAdministrationDeniedError


def member_service() -> MagicMock:
    """個別用例を観測できる fake に固定の公開 DTO だけを返させる。"""

    service = MagicMock(spec=ProjectService)
    member = StoredProjectMember(
        uuid4(), "member@example.test", "Member", ProjectMemberStatus.ACTIVE, datetime.now(UTC),
    )
    service.list_members = AsyncMock(return_value=(
        member, replace(member, status=ProjectMemberStatus.REMOVED),
    ))
    service.add_member = AsyncMock(return_value=member)
    service.remove_member = AsyncMock(return_value=None)
    return service


@pytest.mark.parametrize(("method", "operation"), [
    ("GET", "list_members"), ("PUT", "add_member"), ("DELETE", "remove_member"),
])
def test_members_forward_original_access_and_server_request_uuid_without_public_expansion(
    client: TestClient, method: str, operation: str,
) -> None:
    """correlation は入力 header を信用せず、credential を response/監査 payload と混ぜない。"""

    auth = FakeAuthService()
    service = member_service()
    client.app.state.auth_service = auth
    client.app.state.project_service = service
    cookie = "opaque-test-original-cookie"
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, cookie)
    project_id, user_id, spoofed_request = uuid4(), uuid4(), str(uuid4())
    path = f"/api/v1/projects/{project_id}/members"
    if method != "GET":
        path += f"/{user_id}"
    response = client.request(method, path, headers={"X-Request-ID": spoofed_request})

    assert response.status_code == (204 if method == "DELETE" else 200)
    assert response.headers["cache-control"] == "no-store"
    actual_request = UUID(response.headers["X-Request-ID"])
    assert str(actual_request) != spoofed_request
    call = getattr(service, operation).call_args.kwargs
    access = call["access"]
    assert isinstance(access, UserAccess)
    assert access.actor == auth.actor and access.request_id == actual_request
    assert access.session_token == cookie and access.csrf_token == auth.csrf_token
    assert call["project_id"] == project_id
    if method != "GET":
        assert call["user_id"] == user_id
    if method == "DELETE":
        assert response.content == b""
    else:
        payloads = response.json()["items"] if method == "GET" else [response.json()]
        for payload in payloads:
            assert set(payload) == {"user_id", "email", "display_name", "status", "joined_at"}
        if method == "GET":
            assert set(response.json()) == {"items"}
            assert [item["status"] for item in payloads] == ["ACTIVE", "REMOVED"]


@pytest.mark.parametrize(("method", "operation"), [
    ("GET", "list_members"), ("PUT", "add_member"), ("DELETE", "remove_member"),
])
@pytest.mark.parametrize(("error_type", "status", "code"), [
    (UnauthorizedSessionError, 401, "authentication_required"),
    (CsrfRejectedError, 403, "csrf_rejected"),
    (UserAdministrationDeniedError, 403, "administrator_required"),
    (ProjectNotFoundError, 404, "project_not_found"),
    (ProjectMemberUserNotFoundError, 404, "project_not_found"),
    (ProjectMemberNotFoundError, 404, "project_not_found"),
])
def test_post_entry_member_rejections_have_no_store_shared_problem(
    client: TestClient, method: str, operation: str,
    error_type: type[RuntimeError], status: int, code: str,
) -> None:
    """内部の状態差を body へ漏らさず、再認証の失敗も元の HTTP 境界へ戻す。"""

    service = member_service()
    getattr(service, operation).side_effect = error_type("test internal state")
    client.app.state.project_service = service
    path = f"/api/v1/projects/{uuid4()}/members"
    if method != "GET":
        path += f"/{uuid4()}"
    response = client.request(method, path)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == code
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert "test internal state" not in response.text


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_member_routes_require_admin_before_service_even_with_archived_target(
    client: TestClient, method: str,
) -> None:
    """actor alias が入口を拒否し、USER に管理一覧や操作の存在差を見せない。"""

    auth = FakeAuthService()
    auth.actor = replace(auth.actor, system_role="USER")
    service = member_service()
    client.app.state.auth_service = auth
    client.app.state.project_service = service
    path = f"/api/v1/projects/{uuid4()}/members"
    if method != "GET":
        path += f"/{uuid4()}"
    response = client.request(method, path)
    assert response.status_code == 403
    assert response.json()["code"] == "administrator_required"
    assert response.headers["cache-control"] == "no-store"
    assert service.method_calls == []


def test_member_openapi_declares_no_store_problem_and_unchanged_unpaginated_shape(
    client: TestClient,
) -> None:
    """実 response と一致する schema/headers を全操作へ宣言する。"""

    document = client.app.openapi()
    root = "/api/v1/projects/{project_id}/members"
    for path, method, success in [(root, "get", 200), (root + "/{user_id}", "put", 200),
                                  (root + "/{user_id}", "delete", 204)]:
        operation = document["paths"][path][method]
        assert "auth" in operation["tags"]
        for status in [success, 401, 403, 404, 422]:
            response = operation["responses"][str(status)]
            assert {"Cache-Control", "X-Request-ID"}.issubset(response["headers"])
            if status != success:
                assert response["content"]["application/problem+json"]["schema"] == (
                    PROBLEM_DETAILS_SCHEMA
                )
        assert not {"limit", "offset"}.intersection(
            parameter["name"] for parameter in operation["parameters"]
        )
    for name in ["ProjectMemberResponse", "ProjectMemberListResponse"]:
        assert document["components"]["schemas"][name]["additionalProperties"] is False
