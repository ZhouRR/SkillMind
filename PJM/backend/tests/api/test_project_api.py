"""Project CRUD と membership 管理 API の契約を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fakes import FakeAuthService, FakeProjectService
from fastapi.testclient import TestClient

from projectmind.api.auth_dependencies import project_not_found_problem
from projectmind.api.problems import PROBLEM_DETAILS_SCHEMA
from projectmind.auth.service import AuthenticatedActor, UnauthorizedSessionError
from projectmind.projects import (
    ProjectDeleteBlockedError,
    ProjectNotFoundError,
    ProjectService,
    ProjectStatus,
    StoredProject,
)


def _stored_project(project_id: UUID, status: ProjectStatus) -> StoredProject:
    """Project response の既存 Schema と同じ field を持つ公開 read model を生成する。"""

    now = datetime(2026, 9, 9, tzinfo=UTC)
    return StoredProject(
        project_id=project_id, key="example", name="Example", description="",
        status=status, settings={}, retention_days=90, row_version=1,
        created_at=now, updated_at=now,
    )


@pytest.mark.parametrize("project_status", list(ProjectStatus))
def test_exact_project_detail_reads_active_and_archived_with_authenticated_actor(
    client: TestClient, project_status: ProjectStatus,
) -> None:
    """詳細 endpoint が一覧への後退をせず、同じ actor と ID で認可 service を呼ぶ。"""

    auth = FakeAuthService()
    project_id = uuid4()
    service = MagicMock(spec=ProjectService)
    service.get_project = AsyncMock(return_value=_stored_project(project_id, project_status))
    client.app.state.auth_service = auth
    client.app.state.project_service = service

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 200
    assert response.json()["project_id"] == str(project_id)
    assert response.json()["status"] == project_status.value
    assert set(response.json()) == {
        "project_id", "key", "name", "description", "status", "settings",
        "retention_days", "row_version", "created_at", "updated_at",
    }
    service.get_project.assert_awaited_once_with(actor=auth.actor, project_id=project_id)
    service.list_projects.assert_not_called()


@pytest.mark.parametrize("internal_reason", ["missing", "other organization", "removed member"])
def test_project_detail_hides_the_service_rejection_reason_in_one_not_found_problem(
    client: TestClient, internal_reason: str,
) -> None:
    """内部の拒否理由を HTTP body へ漏らさず共通 404 にする。認可 SQL は別途検証する。"""

    project_id = uuid4()
    service = MagicMock(spec=ProjectService)
    service.get_project = AsyncMock(side_effect=ProjectNotFoundError(internal_reason))
    client.app.state.project_service = service

    response = client.get(f"/api/v1/projects/{project_id}")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "project_not_found"
    assert response.json()["detail"] == project_not_found_problem().detail
    service.get_project.assert_awaited_once()
    service.list_projects.assert_not_called()


def test_project_detail_requires_session_before_lookup(client: TestClient) -> None:
    """詳細 metadata を返す前に共通 Session dependency を適用する。"""

    auth = FakeAuthService()
    auth.authenticate_session = AsyncMock(side_effect=UnauthorizedSessionError("test expired"))
    service = MagicMock(spec=ProjectService)
    client.app.state.auth_service = auth
    client.app.state.project_service = service

    response = client.get(f"/api/v1/projects/{uuid4()}")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    service.get_project.assert_not_called()


@pytest.mark.parametrize(
    ("blocker", "code"),
    [
        ("project_not_archived", "project_delete_requires_archive"),
        ("run_history_exists", "project_delete_blocked_by_runs"),
        ("task_schedule_exists", "project_delete_blocked_by_schedules"),
        ("member_audit_exists", "project_delete_blocked_by_member_audit"),
    ],
)
def test_project_delete_returns_a_distinct_stable_conflict_for_each_reference(
    client: TestClient, blocker: str, code: str,
) -> None:
    """Run と Schedule を区別した拒否を伝え、route が参照を削除しない。"""

    service = MagicMock(spec=ProjectService)
    service.delete_project = AsyncMock(side_effect=ProjectDeleteBlockedError(
        "Project cannot be deleted", blockers=(blocker,),
    ))
    client.app.state.project_service = service

    response = client.delete(f"/api/v1/projects/{uuid4()}?expected_row_version=1")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    service.delete_project.assert_awaited_once()
    assert len(service.method_calls) == 1


def test_project_detail_and_delete_openapi_declare_the_actual_problem_contract(
    client: TestClient,
) -> None:
    """既存 v1 Problem body と拒否 code が公開詳細/削除の HTTP 声明に残る。"""

    path = client.app.openapi()["paths"]["/api/v1/projects/{project_id}"]
    for method, statuses in [("get", [401, 404, 422]), ("delete", [401, 403, 404, 409, 422])]:
        for code in statuses:
            response = path[method]["responses"][str(code)]
            assert response["content"]["application/problem+json"]["schema"] == (
                PROBLEM_DETAILS_SCHEMA
            )
            assert "application/json" not in response["content"]
    assert "project_delete_blocked_by_schedules" in (
        path["delete"]["responses"]["409"]["description"]
    )


def test_admin_can_create_list_archive_and_assign_project_member(client: TestClient) -> None:
    """ADMIN の Project CRUD と membership API が明示 field だけを返す。"""

    auth = FakeAuthService()
    projects = FakeProjectService()
    client.app.state.auth_service = auth
    client.app.state.project_service = projects
    write_headers = {"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token}

    created = client.post(
        "/api/v1/projects",
        headers=write_headers,
        json={
            "key": "quality-team",
            "name": "Quality Team",
            "description": "JAF quality analysis",
            "settings": {"timezone": "Asia/Tokyo"},
            "retention_days": 90,
        },
    )

    assert created.status_code == 201
    project_id = created.json()["project_id"]
    assert "organization_id" not in created.json()
    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert listed.json()["items"] == [created.json()]

    user_id = uuid4()
    member = client.put(
        f"/api/v1/projects/{project_id}/members/{user_id}",
        headers=write_headers,
    )
    assert member.status_code == 200
    assert member.json()["user_id"] == str(user_id)
    assert "password_hash" not in member.json()

    archived = client.post(
        f"/api/v1/projects/{project_id}/archive",
        headers=write_headers,
        json={"expected_row_version": 1},
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"
    assert client.get("/api/v1/projects").json()["items"] == []


def test_archived_project_can_be_listed_restored_and_deleted(client: TestClient) -> None:
    """Archive 後も一覧・復元・削除の経路が残り、key を取り戻せることを確認する。

    Archive しただけでは key の一意制約が解けないため、利用者が同じ key で作り直せなくなる。
    ここは「見えない・消せない・作れない」の三重詰まりを防ぐ回帰点になる。
    """

    auth = FakeAuthService()
    projects = FakeProjectService()
    client.app.state.auth_service = auth
    client.app.state.project_service = projects
    write_headers = {"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token}

    project_id = client.post(
        "/api/v1/projects",
        headers=write_headers,
        json={
            "key": "quality-team",
            "name": "Quality Team",
            "description": "",
            "settings": {},
            "retention_days": 90,
        },
    ).json()["project_id"]
    client.post(
        f"/api/v1/projects/{project_id}/archive", headers=write_headers,
        json={"expected_row_version": 1},
    )

    # 既定一覧からは消えるが、include_archived で監査対象として取り出せる。
    assert client.get("/api/v1/projects").json()["items"] == []
    archived = client.get("/api/v1/projects?include_archived=true").json()["items"]
    assert [item["status"] for item in archived] == ["ARCHIVED"]

    restored = client.post(
        f"/api/v1/projects/{project_id}/unarchive", headers=write_headers,
        json={"expected_row_version": 2},
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "ACTIVE"
    assert len(client.get("/api/v1/projects").json()["items"]) == 1

    # ACTIVE のまま削除しようとした場合は archive 手順を促す 409 に落ちる。
    blocked = client.request(
        "DELETE", f"/api/v1/projects/{project_id}?expected_row_version=3", headers=write_headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "project_delete_requires_archive"

    client.post(
        f"/api/v1/projects/{project_id}/archive", headers=write_headers,
        json={"expected_row_version": 3},
    )
    deleted = client.request(
        "DELETE", f"/api/v1/projects/{project_id}?expected_row_version=4", headers=write_headers,
    )
    assert deleted.status_code == 204
    assert client.get("/api/v1/projects?include_archived=true").json()["items"] == []


def test_user_project_mutation_returns_administrator_required(client: TestClient) -> None:
    """USER が Project 管理 endpoint を直接呼んでも 403 で拒否される。"""

    auth = FakeAuthService()
    auth.actor = AuthenticatedActor(
        user_id=auth.actor.user_id,
        organization_id=auth.actor.organization_id,
        email=auth.actor.email,
        display_name=auth.actor.display_name,
        system_role="USER",
    )
    client.app.state.auth_service = auth
    client.app.state.project_service = FakeProjectService()

    response = client.post(
        "/api/v1/projects",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"key": "denied", "name": "Denied", "retention_days": 90},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "administrator_required"
