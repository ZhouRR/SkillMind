"""Project CRUD と membership 管理 API の契約を検証する。"""

from __future__ import annotations

from uuid import uuid4

from fakes import FakeAuthService, FakeProjectService
from fastapi.testclient import TestClient

from projectmind.auth.service import AuthenticatedActor


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
    client.post(f"/api/v1/projects/{project_id}/archive", headers=write_headers)

    # 既定一覧からは消えるが、include_archived で監査対象として取り出せる。
    assert client.get("/api/v1/projects").json()["items"] == []
    archived = client.get("/api/v1/projects?include_archived=true").json()["items"]
    assert [item["status"] for item in archived] == ["ARCHIVED"]

    restored = client.post(f"/api/v1/projects/{project_id}/unarchive", headers=write_headers)
    assert restored.status_code == 200
    assert restored.json()["status"] == "ACTIVE"
    assert len(client.get("/api/v1/projects").json()["items"]) == 1

    # ACTIVE のまま削除しようとした場合は archive 手順を促す 409 に落ちる。
    blocked = client.request(
        "DELETE", f"/api/v1/projects/{project_id}", headers=write_headers
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "project_delete_requires_archive"

    client.post(f"/api/v1/projects/{project_id}/archive", headers=write_headers)
    deleted = client.request(
        "DELETE", f"/api/v1/projects/{project_id}", headers=write_headers
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
