"""調度管理の原 credential 接続と HTTP 拒否を検証し、実 DB の競争証明と区別する。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import FakeAuthService, FakeProjectAuthorizationService, FakeScheduleService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from projectmind.api.problems import PROBLEM_DETAILS_SCHEMA
from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.projects.domain import (
    ProjectArchivedError,
    ProjectNotFoundError,
    ProjectStatus,
    StoredProject,
)
from projectmind.schedules.domain import (
    InvalidScheduleTransitionError,
    ScheduleConflictError,
    ScheduleInvalidError,
    ScheduleNotFoundError,
)
from projectmind.users.domain import UserAccess

_METHODS = {
    "create": "create_schedule",
    "update": "update_schedule",
    "status": "change_status",
}


def application(client: TestClient) -> FastAPI:
    """共有 fixture の app だけへ fake を配置し、別 lifespan や接続を作らない。"""

    assert isinstance(client.app, FastAPI)
    return client.app


def write_request(action: str) -> tuple[str, str, dict[str, Any]]:
    """既存の公開 body のまま、対象を固定した一回の管理要求を組み立てる。"""

    base = f"/api/v1/projects/{uuid4()}/schedules"
    if action == "status":
        return "POST", f"{base}/{uuid4()}/status", {"status": "PAUSED", "expected_row_version": 7}
    body: dict[str, Any] = {
        "name": "nightly",
        "definition": {"kind": "CRON", "timezone": "UTC", "cron_expression": "0 3 * * *"},
        "input": {"query": "fixture"},
        "sources": {},
    }
    if action == "create":
        body.update(skill_version_id=str(uuid4()), task_key="analyze")
        return "POST", base, body
    body["expected_row_version"] = 7
    return "PUT", f"{base}/{uuid4()}", body


def install_writer(client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str) -> AsyncMock:
    """本物の route から呼ばれる一つの service 境界だけを記録する。"""

    service = FakeScheduleService()
    writer = AsyncMock(wraps=getattr(service, _METHODS[action]))
    monkeypatch.setattr(service, _METHODS[action], writer)
    application(client).state.schedule_service = service
    return writer


@pytest.mark.parametrize("action", list(_METHODS))
@pytest.mark.parametrize("role", ["ADMIN", "USER"])
def test_writes_forward_each_original_session_actor_csrf_and_server_request_uuid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str, role: str
) -> None:
    """同じ actor の別会話でも原値を取り直し、外来 request ID や入口 DTO だけに置換しない。"""

    app = application(client)
    auth = cast(FakeAuthService, app.state.auth_service)
    auth.actor = replace(auth.actor, system_role=role)
    projects = cast(FakeProjectAuthorizationService, app.state.project_service)
    project_read = AsyncMock(wraps=projects.get_project)
    monkeypatch.setattr(projects, "get_project", project_read)
    authentication = AsyncMock(wraps=auth.authenticate_unsafe_session)
    monkeypatch.setattr(auth, "authenticate_unsafe_session", authentication)
    writer = install_writer(client, monkeypatch, action)
    method, url, body = write_request(action)
    external_request_id = str(uuid4())
    accesses: list[UserAccess] = []

    for _ in range(2):
        credentials = generate_session_credentials()
        auth.session_token = credentials.session_token
        auth.csrf_token = credentials.csrf_token
        client.cookies.set(app.state.settings.auth_session_cookie_name, auth.session_token)
        response = client.request(
            method,
            url,
            json=body,
            headers={"X-CSRF-Token": auth.csrf_token, "X-Request-ID": external_request_id},
        )
        assert response.status_code == (201 if action == "create" else 200)
        assert response.headers["Cache-Control"] == "no-store"
        assert writer.await_args is not None
        received = writer.await_args.kwargs
        access = received["access"]
        assert isinstance(access, UserAccess)
        assert access.actor is auth.actor
        assert access.session_token == credentials.session_token
        assert access.csrf_token == credentials.csrf_token
        assert access.request_id.version == 4
        assert str(access.request_id) == response.headers["X-Request-ID"]
        assert str(access.request_id) != external_request_id
        assert "actor" not in received
        assert received["project_id"] == UUID(url.split("/")[4])
        if action != "create":
            assert received["schedule_id"] == UUID(url.split("/")[6])
            assert received["expected_row_version"] == 7
        else:
            assert response.json()["created_by"] == str(auth.actor.user_id)
        authentication.assert_awaited_with(
            session_token=credentials.session_token, csrf_token=credentials.csrf_token
        )
        assert credentials.session_token not in response.text
        assert credentials.csrf_token not in response.text
        accesses.append(access)

    assert writer.await_count == authentication.await_count == project_read.await_count == 2
    assert accesses[0].session_token != accesses[1].session_token
    assert accesses[0].request_id != accesses[1].request_id


@pytest.mark.parametrize("action", list(_METHODS))
@pytest.mark.parametrize(
    "error,status,code",
    [
        (UnauthorizedSessionError("private session reason"), 401, "authentication_required"),
        (CsrfRejectedError("private proof reason"), 403, "csrf_rejected"),
        (ProjectNotFoundError("private missing reason"), 404, "project_not_found"),
        (ProjectNotFoundError("private organization reason"), 404, "project_not_found"),
        (ProjectNotFoundError("private membership reason"), 404, "project_not_found"),
        (ProjectArchivedError("private archive reason"), 409, "project_archived"),
    ],
)
def test_transaction_access_failures_use_sanitized_shared_problems(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """入口で許可されても business 側の拒否を保持し、内部理由や原 credential を公開しない。"""

    writer = install_writer(client, monkeypatch, action)
    writer.side_effect = error
    method, url, body = write_request(action)
    response = client.request(method, url, json=body)
    assert response.status_code == status
    assert response.headers["Content-Type"] == "application/problem+json"
    assert response.headers["Cache-Control"] == "no-store"
    problem = response.json()
    assert problem["code"] == code
    assert problem["status"] == status
    assert problem["request_id"] == response.headers["X-Request-ID"]
    assert str(error) not in response.text
    writer.assert_awaited_once()


@pytest.mark.parametrize("action", list(_METHODS))
@pytest.mark.parametrize("failure", ["origin", "csrf", "session", "project", "archive"])
def test_original_project_write_dependency_rejects_before_the_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str, failure: str
) -> None:
    """transaction 再検証を足しても、unsafe Origin/CSRF と Project の入口を省略しない。"""

    app = application(client)
    writer = install_writer(client, monkeypatch, action)
    auth = cast(FakeAuthService, app.state.auth_service)
    code, status = "csrf_rejected", 403
    if failure == "origin":
        client.headers["Origin"] = "https://foreign.invalid"
    elif failure == "csrf":
        client.headers["X-CSRF-Token"] = "invalid"
    elif failure == "session":
        auth.unauthorized = True
        code, status = "authentication_required", 401
    else:
        projects = cast(FakeProjectAuthorizationService, app.state.project_service)

        async def get_project(*, actor: AuthenticatedActor, project_id: UUID) -> StoredProject:
            """実 Project には接続せず、入口で確定した権限拒否を模倣する。"""

            if failure == "project":
                raise ProjectNotFoundError("private project reason")
            current = await FakeProjectAuthorizationService().get_project(
                actor=actor, project_id=project_id
            )
            return replace(current, status=ProjectStatus.ARCHIVED)

        monkeypatch.setattr(projects, "get_project", get_project)
        code, status = (
            ("project_not_found", 404) if failure == "project" else ("project_archived", 409)
        )
    method, url, body = write_request(action)
    response = client.request(method, url, json=body)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    writer.assert_not_awaited()


@pytest.mark.parametrize("action", list(_METHODS))
def test_unknown_database_failure_is_not_reclassified_as_access_rejection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """提交の不明な DB 例外を確定した 401/403/404/409 や成功へ変換しない。"""

    writer = install_writer(client, monkeypatch, action)
    failure = OperationalError("synthetic statement", {}, RuntimeError("synthetic DB failure"))
    writer.side_effect = failure
    method, url, body = write_request(action)
    with pytest.raises(OperationalError) as observed:
        client.request(method, url, json=body)
    assert observed.value is failure
    writer.assert_awaited_once()


@pytest.mark.parametrize("action", list(_METHODS))
def test_current_task_binding_rejection_does_not_save_or_retry_schedule(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """保存/復帰 TX の確定した Task 失効を既存の入力 Problem として一回だけ返す。"""

    writer = install_writer(client, monkeypatch, action)
    writer.side_effect = ScheduleInvalidError("Published task is not available")
    method, url, body = write_request(action)
    if action == "status":
        body["status"] = "ACTIVE"
    response = client.request(method, url, json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "schedule_invalid"
    assert response.json()["detail"] == "Published task is not available"
    assert response.headers["Content-Type"] == "application/problem+json"
    assert response.headers["Cache-Control"] == "no-store"
    writer.assert_awaited_once()


@pytest.mark.parametrize(
    "action,error,status,code",
    [
        ("create", ScheduleInvalidError("invalid task"), 422, "schedule_invalid"),
        ("update", ScheduleNotFoundError("missing"), 404, "schedule_not_found"),
        ("update", ScheduleConflictError("stale"), 409, "schedule_conflict"),
        ("update", ScheduleInvalidError("terminal"), 422, "schedule_invalid"),
        ("status", ScheduleNotFoundError("missing"), 404, "schedule_not_found"),
        ("status", ScheduleConflictError("stale"), 409, "schedule_conflict"),
        ("status", InvalidScheduleTransitionError("terminal"), 409, "schedule_transition_invalid"),
        ("status", ScheduleInvalidError("no future"), 422, "schedule_invalid"),
    ],
)
def test_existing_schedule_business_rejections_remain_distinct(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """新しい授権拒否が原版競合・終態・入力の従来分類を上書きしない。"""

    writer = install_writer(client, monkeypatch, action)
    writer.side_effect = error
    method, url, body = write_request(action)
    response = client.request(method, url, json=body)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    writer.assert_awaited_once()


@pytest.mark.parametrize("action", list(_METHODS))
def test_write_openapi_preserves_body_and_declares_handled_problems_and_no_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """公開 request に credential を追加せず、実際の成功/拒否 media type と header を宣言する。"""

    writer = install_writer(client, monkeypatch, action)
    suffix = {"create": "", "update": "/{schedule_id}", "status": "/{schedule_id}/status"}[action]
    method = "put" if action == "update" else "post"
    path = f"/api/v1/projects/{{project_id}}/schedules{suffix}"
    operation = application(client).openapi()["paths"][path][method]
    model = {
        "create": "CreateScheduleRequest",
        "update": "UpdateScheduleRequest",
        "status": "ScheduleStatusRequest",
    }[action]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": f"#/components/schemas/{model}"
    }
    success = "201" if action == "create" else "200"
    assert set(operation["responses"]) == {success, "401", "403", "404", "409", "422"}
    for code, declaration in operation["responses"].items():
        assert declaration["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
        assert declaration["headers"]["X-Request-ID"]["required"] is True
        if code != success:
            assert set(declaration["content"]) == {"application/problem+json"}
            assert (
                declaration["content"]["application/problem+json"]["schema"]
                == PROBLEM_DETAILS_SCHEMA
            )
    request_method, url, _ = write_request(action)
    response = client.request(request_method, url, json={})
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    writer.assert_not_awaited()
