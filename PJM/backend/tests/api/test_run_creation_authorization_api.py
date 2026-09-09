"""普通 Run 作成の原 credential 接続と拒否を検証し、実 DB の認証競争と区別する。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fakes import FakeAuthService, FakeProjectAuthorizationService, FakeRunService, FakeSkillService
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
from projectmind.runs.domain import IdempotencyConflictError, TaskSourceSelectionError
from projectmind.users.domain import UserAccess

_PATHS = ("lookup", "create", "missing_recheck", "invalid_recheck")


def application(client: TestClient) -> FastAPI:
    """既存の接続なし fixture にだけ fake を配置し、別 lifespan を起動しない。"""

    assert isinstance(client.app, FastAPI)
    return client.app


def creation_request() -> tuple[str, dict[str, Any], dict[str, str]]:
    """原意図の空白・配列順を残し、HTTP credential を body と混ぜない。"""

    return (
        f"/api/v1/projects/{uuid4()}/task-runs",
        {
            "skill_version_id": str(uuid4()),
            "task_key": "review-change",
            "input": {"target": " main ", "ordered": ["second", "first"], "optional": None},
            "sources": {"repository-source": "git"},
        },
        {"Idempotency-Key": "Original-Request_001"},
    )


def install_creation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, *, replay: bool = False
) -> tuple[AsyncMock, AsyncMock]:
    """実 route を通し、RunService の二つの transaction 入口だけを観測する。"""

    service = FakeRunService(replay=replay)
    lookup = AsyncMock(wraps=service.find_task_run_replay)
    create = AsyncMock(wraps=service.create_task_run)
    monkeypatch.setattr(service, "find_task_run_replay", lookup)
    monkeypatch.setattr(service, "create_task_run", create)
    app = application(client)
    app.state.run_service = service
    app.state.skill_service = FakeSkillService()
    return lookup, create


def inject_failure(
    client: TestClient, path: str, lookup: AsyncMock, create: AsyncMock, error: Exception
) -> None:
    """入口後または解析後の失効を選び、route が自動再試行しないことを確認する。"""

    if path == "create":
        create.side_effect = error
    elif path == "lookup":
        lookup.side_effect = error
    else:
        application(client).state.skill_service = FakeSkillService(
            published_task_missing=path == "missing_recheck",
            task_input_invalid=path == "invalid_recheck",
        )
        lookup.side_effect = [None, error]


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("role", ["ADMIN", "USER"])
def test_create_and_replay_forward_original_credentials_for_two_sessions_of_one_actor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, replay: bool, role: str
) -> None:
    """同じ actor の別会話を区別し、原 body/key と server UUID を各 transaction へ渡す。"""

    app = application(client)
    auth = cast(FakeAuthService, app.state.auth_service)
    auth.actor = replace(auth.actor, system_role=role)
    authentication = AsyncMock(wraps=auth.authenticate_unsafe_session)
    monkeypatch.setattr(auth, "authenticate_unsafe_session", authentication)
    lookup, create = install_creation(client, monkeypatch, replay=replay)
    url, body, headers = creation_request()
    external_id = str(uuid4())
    accesses: list[UserAccess] = []

    for _ in range(2):
        credentials = generate_session_credentials()
        auth.session_token, auth.csrf_token = credentials.session_token, credentials.csrf_token
        client.cookies.set(app.state.settings.auth_session_cookie_name, auth.session_token)
        response = client.post(
            url,
            json=body,
            headers={**headers, "X-CSRF-Token": auth.csrf_token, "X-Request-ID": external_id},
        )
        assert response.status_code == (200 if replay else 201)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Idempotent-Replay"] == str(replay).lower()
        assert response.headers["Location"] == (
            f"/projectmind/api/v1/runs/{response.json()['run_id']}"
        )
        assert response.json()["idempotent_replay"] is replay
        assert lookup.await_args is not None
        received = lookup.await_args.kwargs
        access = received["authorization"]
        assert isinstance(access, UserAccess)
        assert access.actor is auth.actor
        assert access.session_token == credentials.session_token
        assert access.csrf_token == credentials.csrf_token
        assert access.request_id.version == 4
        assert str(access.request_id) == response.headers["X-Request-ID"]
        assert str(access.request_id) != external_id
        assert received == {
            "authorization": access,
            "project_id": UUID(url.split("/")[4]),
            "skill_version_id": UUID(body["skill_version_id"]),
            "task_key": body["task_key"],
            "input_json": body["input"],
            "sources": body["sources"],
            "actor_id": auth.actor.user_id,
            "idempotency_key": headers["Idempotency-Key"],
        }
        if not replay:
            assert create.await_args is not None
            created = create.await_args.kwargs
            assert created["authorization"] is access
            assert created["trace_id"] == str(access.request_id)
            assert created["input_json"] == body["input"]
            assert created["sources"] == body["sources"]
            assert created["idempotency_key"] == headers["Idempotency-Key"]
            assert created["actor_id"] == auth.actor.user_id
            assert not {"participant", "actor_system_role", "project_membership"} & created.keys()
        authentication.assert_awaited_with(
            session_token=credentials.session_token, csrf_token=credentials.csrf_token
        )
        assert credentials.session_token not in response.text
        assert credentials.csrf_token not in response.text
        accesses.append(access)

    assert lookup.await_count == authentication.await_count == 2
    assert create.await_count == (0 if replay else 2)
    assert accesses[0].actor is accesses[1].actor
    assert accesses[0].session_token != accesses[1].session_token
    assert accesses[0].request_id != accesses[1].request_id


@pytest.mark.parametrize("path", _PATHS)
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
def test_each_creation_transaction_rejection_uses_sanitized_shared_problem(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """二度目の原要求確認の失効も、古い task 解析エラーや成功へ置換しない。"""

    lookup, create = install_creation(client, monkeypatch)
    inject_failure(client, path, lookup, create, error)
    url, body, headers = creation_request()
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == status
    assert response.headers["Content-Type"] == "application/problem+json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["code"] == code
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert str(error) not in response.text
    assert "Location" not in response.headers
    assert "Idempotent-Replay" not in response.headers
    assert lookup.await_count == (2 if path.endswith("recheck") else 1)
    assert create.await_count == (1 if path == "create" else 0)
    if path.endswith("recheck"):
        first, second = lookup.await_args_list
        assert first == second
        assert first.kwargs["authorization"] is second.kwargs["authorization"]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("database_error", [False, True])
def test_unknown_creation_failure_propagates_without_replay_or_known_rejection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, database_error: bool
) -> None:
    """提交未知や予期しない障害は、401/409 や原 Run の自動確認へ縮めない。"""

    lookup, create = install_creation(client, monkeypatch)
    failure = (
        OperationalError("synthetic statement", {}, RuntimeError("synthetic DB failure"))
        if database_error
        else RuntimeError("synthetic unknown result")
    )
    inject_failure(client, path, lookup, create, failure)
    url, body, headers = creation_request()
    with pytest.raises(type(failure)) as observed:
        client.post(url, json=body, headers=headers)
    assert observed.value is failure
    assert lookup.await_count == (2 if path.endswith("recheck") else 1)
    assert create.await_count == (1 if path == "create" else 0)


@pytest.mark.parametrize("failure", ["origin", "csrf", "session", "project", "archive"])
def test_creation_keeps_project_write_dependency_before_any_original_request_lookup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """既存 Run の確認だけでも Origin/CSRF と現在 Project の入口を省略しない。"""

    app = application(client)
    lookup, create = install_creation(client, monkeypatch, replay=True)
    auth = cast(FakeAuthService, app.state.auth_service)
    code, expected = "csrf_rejected", 403
    if failure == "origin":
        client.headers["Origin"] = "https://foreign.invalid"
    elif failure == "csrf":
        client.headers["X-CSRF-Token"] = "invalid"
    elif failure == "session":
        auth.unauthorized = True
        code, expected = "authentication_required", 401
    else:
        projects = cast(FakeProjectAuthorizationService, app.state.project_service)

        async def get_project(*, actor: AuthenticatedActor, project_id: UUID) -> StoredProject:
            """実 Project を読まず、入口の不存在または帰档を選択する。"""

            if failure == "project":
                raise ProjectNotFoundError("private project reason")
            current = await FakeProjectAuthorizationService().get_project(
                actor=actor, project_id=project_id
            )
            return replace(current, status=ProjectStatus.ARCHIVED)

        monkeypatch.setattr(projects, "get_project", get_project)
        code, expected = (
            ("project_not_found", 404) if failure == "project" else ("project_archived", 409)
        )
    url, body, headers = creation_request()
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == expected
    assert response.json()["code"] == code
    assert response.headers["Cache-Control"] == "no-store"
    lookup.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize(
    "error,status,code",
    [
        (IdempotencyConflictError("different request"), 409, "idempotency_conflict"),
        (TaskSourceSelectionError("invalid source"), 422, "task_source_selection_invalid"),
    ],
)
def test_original_creation_business_rejections_remain_distinct(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """共通認証例外を追加しても、原意図の競合と資源選択の分類を変更しない。"""

    lookup, create = install_creation(client, monkeypatch)
    inject_failure(client, path, lookup, create, error)
    url, body, headers = creation_request()
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("invalid", ["missing_key", "extra_access", "extra_actor", "extra_role"])
def test_creation_public_request_shape_stays_closed_and_validation_is_not_cached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    """内部認証 parameter を公開 body へ持ち込まず、旧 header 必須条件も保持する。"""

    lookup, create = install_creation(client, monkeypatch)
    url, body, headers = creation_request()
    if invalid == "missing_key":
        headers = {}
    else:
        body[invalid.removeprefix("extra_")] = "untrusted input"
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Type"] == "application/problem+json"
    lookup.assert_not_awaited()
    create.assert_not_awaited()


def test_creation_openapi_keeps_public_body_and_declares_status_headers_and_problems(
    client: TestClient,
) -> None:
    """新 field は公開せず、201/200 と既知拒否の media type/no-store を明示する。"""

    schema = application(client).openapi()
    operation = schema["paths"]["/api/v1/projects/{project_id}/task-runs"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreateTaskRunRequest"
    }
    body_schema = schema["components"]["schemas"]["CreateTaskRunRequest"]
    assert set(body_schema["properties"]) == {"skill_version_id", "task_key", "input", "sources"}
    assert body_schema["required"] == ["skill_version_id", "task_key"]
    assert body_schema["additionalProperties"] is False
    assert set(operation["responses"]) == {"200", "201", "401", "403", "404", "409", "422"}
    for code, declaration in operation["responses"].items():
        assert declaration["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
        assert declaration["headers"]["X-Request-ID"]["required"] is True
        if code in {"200", "201"}:
            assert declaration["headers"]["Location"]["required"] is True
            assert declaration["headers"]["Idempotent-Replay"]["schema"]["const"] == (
                "true" if code == "200" else "false"
            )
            assert declaration["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/RunResponse"
            }
        else:
            assert set(declaration["content"]) == {"application/problem+json"}
            assert (
                declaration["content"]["application/problem+json"]["schema"]
                == PROBLEM_DETAILS_SCHEMA
            )
