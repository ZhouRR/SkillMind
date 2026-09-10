"""原版と原会話の HTTP 接線、no-store と安定した競合契約を確認する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from skillmind.api.problems import PROBLEM_DETAILS_SCHEMA
from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.projects.domain import (
    ProjectStatus,
    ProjectVersionConflictError,
    ProjectVersionExhaustedError,
    StoredProject,
)
from skillmind.projects.service import ProjectService
from skillmind.users.domain import UserAccess
from tests.api.fakes import FakeAuthService


def stored(project_id: UUID) -> StoredProject:
    """秘密や内部 ORM field を含まない版付き Project DTO を返す。"""

    now = datetime.now(UTC)
    return StoredProject(
        project_id=project_id,
        key="project",
        name="Project",
        description="",
        status=ProjectStatus.ACTIVE,
        settings={},
        retention_days=90,
        row_version=1,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("operation", ["create", "update", "archive", "unarchive", "delete"])
def test_all_project_write_routes_forward_access_and_exact_expected_version(
    client: TestClient,
    operation: str,
) -> None:
    """元 credential/サーバー request UUID を用例へ渡し、HTTP 入力の版を差し替えない。"""

    auth = FakeAuthService()
    service = MagicMock(spec=ProjectService)
    project_id = uuid4()
    target = AsyncMock(return_value=None if operation == "delete" else stored(project_id))
    setattr(service, f"{operation}_project", target)
    client.app.state.auth_service = auth
    client.app.state.project_service = service
    original_cookie = "test-original-cookie"
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, original_cookie)
    path = f"/api/v1/projects/{project_id}"
    if operation == "create":
        response = client.post(
            "/api/v1/projects",
            json={"key": "project", "name": "Project", "retention_days": 90},
        )
    elif operation == "delete":
        response = client.delete(path + "?expected_row_version=7")
    elif operation == "update":
        response = client.patch(path, json={"expected_row_version": 7, "description": ""})
    else:
        response = client.post(path + f"/{operation}", json={"expected_row_version": 7})
    assert response.status_code == (
        201 if operation == "create" else 204 if operation == "delete" else 200
    )
    assert response.headers["cache-control"] == "no-store"
    kwargs = target.call_args.kwargs
    access = kwargs["access"]
    assert isinstance(access, UserAccess) and access.actor == auth.actor
    assert access.session_token == original_cookie and access.csrf_token == auth.csrf_token
    assert access.request_id == UUID(response.headers["X-Request-ID"])
    if operation == "update":
        assert kwargs["command"].expected_row_version == 7
        assert kwargs["command"].description == ""
    elif operation != "create":
        assert kwargs["expected_row_version"] == 7
    if operation != "create":
        assert kwargs["project_id"] == project_id
    if operation != "delete":
        assert response.json()["row_version"] == 1


@pytest.mark.parametrize("operation", ["update", "archive", "unarchive", "delete"])
def test_old_clients_without_version_fail_before_service(
    client: TestClient, operation: str
) -> None:
    """旧 body/query を現版で救済せず、明示 422 で拒否する。"""

    service = MagicMock(spec=ProjectService)
    client.app.state.project_service = service
    path = f"/api/v1/projects/{uuid4()}"
    if operation == "delete":
        response = client.delete(path)
    elif operation == "update":
        response = client.patch(path, json={"name": "Changed"})
    else:
        response = client.post(path + f"/{operation}", json={})
    assert response.status_code == 422 and service.method_calls == []
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    "query",
    [
        "expected_row_version=1&expected_row_version=2",
        "expected_row_version=1&expected_row_version=1",
        "expected_row_version=1.0",
        "expected_row_version=true",
        "expected_row_version=",
        "expected_row_version=2147483648",
    ],
)
def test_delete_rejects_ambiguous_or_non_integer_version_query(
    client: TestClient, query: str
) -> None:
    """FastAPI の最後値採用や小数の整数変換で、利用者の元版を曖昧にしない。"""

    service = MagicMock(spec=ProjectService)
    client.app.state.project_service = service
    response = client.delete(f"/api/v1/projects/{uuid4()}?{query}")
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert service.method_calls == []


@pytest.mark.parametrize("operation", ["create", "update", "archive", "unarchive", "delete"])
def test_late_session_rejection_keeps_shared_auth_problem(
    client: TestClient, operation: str
) -> None:
    """入口成功後の失効も、内部 DB/credential 本文を出さない共通 401 として返す。"""

    service = MagicMock(spec=ProjectService)
    setattr(
        service, f"{operation}_project", AsyncMock(side_effect=UnauthorizedSessionError("private"))
    )
    client.app.state.project_service = service
    path = f"/api/v1/projects/{uuid4()}"
    if operation == "create":
        response = client.post(
            "/api/v1/projects", json={"key": "new", "name": "New", "retention_days": 90}
        )
    elif operation == "delete":
        response = client.delete(path + "?expected_row_version=1")
    elif operation == "update":
        response = client.patch(path, json={"expected_row_version": 1, "name": "Updated"})
    else:
        response = client.post(path + f"/{operation}", json={"expected_row_version": 1})
    assert response.status_code == 401 and response.json()["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store" and "private" not in response.text


@pytest.mark.parametrize("error_type", [ProjectVersionConflictError, ProjectVersionExhaustedError])
@pytest.mark.parametrize("operation", ["update", "archive", "unarchive"])
def test_project_version_conflicts_have_distinct_stable_codes(
    client: TestClient,
    error_type: type[RuntimeError],
    operation: str,
) -> None:
    """元版の衝突と上限到達を区別し、最新の版を勝手に返して再送を促さない。"""

    service = MagicMock(spec=ProjectService)
    setattr(service, f"{operation}_project", AsyncMock(side_effect=error_type("private")))
    client.app.state.project_service = service
    path = f"/api/v1/projects/{uuid4()}"
    if operation == "update":
        response = client.patch(path, json={"expected_row_version": 1, "name": "Updated"})
    else:
        response = client.post(path + f"/{operation}", json={"expected_row_version": 1})
    assert response.status_code == 409
    expected = (
        "project_version_conflict"
        if error_type is ProjectVersionConflictError
        else "project_version_exhausted"
    )
    assert response.json()["code"] == expected and "private" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_project_openapi_covers_versions_no_store_and_complete_problem_shapes(
    client: TestClient,
) -> None:
    """公開 project の全操作に成功/拒否の cache 境界を宣言し、DTO 範囲も Schema と揃える。"""

    document = client.app.openapi()
    for path, operations in document["paths"].items():
        if path not in {
            "/api/v1/projects",
            "/api/v1/projects/{project_id}",
            "/api/v1/projects/{project_id}/archive",
            "/api/v1/projects/{project_id}/unarchive",
        }:
            continue
        for operation in operations.values():
            assert operation["tags"] == ["projects", "auth"]
            for code, response in operation["responses"].items():
                assert {"Cache-Control", "X-Request-ID"}.issubset(response["headers"])
                if int(code) >= 400:
                    assert response["content"] == {
                        "application/problem+json": {"schema": PROBLEM_DETAILS_SCHEMA}
                    }
    schema = document["components"]["schemas"]["ProjectResponse"]
    assert "row_version" in schema["required"] and schema["additionalProperties"] is False
    assert schema["properties"]["row_version"]["maximum"] == 2147483647
    assert schema["properties"]["name"]["maxLength"] == 200
    assert schema["properties"]["retention_days"]["maximum"] == 3650
