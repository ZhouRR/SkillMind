"""Composition の原資格接続と静的 Problem を検証し、実 DB の競争証明と区別する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, Protocol
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy.exc import OperationalError

from skillmind.api.problems import PROBLEM_DETAILS_SCHEMA
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.compositions.domain import (
    ModuleNotFoundError,
    ModuleSkillInvalidError,
    ModuleValidationError,
)
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.users.domain import UserAdministrationDeniedError
from tests.api.fakes import FakeAuthService, FakeCompositionService
from tests.api.test_skill_draft_api import HttpResponse, assert_problem
from tests.compositions.composition_authorization_harness import CompositionSession

Operation = Literal["create", "update", "delete"]
OPERATIONS: tuple[Operation, ...] = ("create", "update", "delete")
_PRIVATE_MARKER = "Synthetic private aggregate detail must never appear in HTTP"


class ModuleHttpResponse(HttpResponse, Protocol):
    """共通 Problem 検査に加え、204 の原 byte と非漏洩を検証する構造。"""

    @property
    def content(self) -> bytes:
        """HTTP が実際に返した byte 列を提供する。"""
        ...

    @property
    def text(self) -> str:
        """本文へ私有値が混入していないか検証する。"""
        ...


def application(client: TestClient) -> FastAPI:
    """共有 fixture の app のみを利用し、追加 lifespan や外部接続を作らない。"""

    assert isinstance(client.app, FastAPI)
    return client.app


def request_operation(
    client: TestClient,
    operation: Operation,
    *,
    project_id: UUID | None = None,
    module_id: UUID | None = None,
    version_ids: list[UUID] | None = None,
) -> ModuleHttpResponse:
    """既存 body と原 cookie/header を三つの公開 mutation へ渡す。"""

    path = f"/api/v1/projects/{project_id or uuid4()}/modules"
    if operation == "delete":
        return client.delete(f"{path}/{module_id or uuid4()}")
    body = {
        "name": "Synthetic module",
        "description": "Synthetic module description",
        "skill_version_ids": [str(value) for value in version_ids or [uuid4()]],
    }
    if operation == "create":
        return client.post(path, json=body)
    return client.put(f"{path}/{module_id or uuid4()}", json=body)


def install_writer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: Operation
) -> AsyncMock:
    """エラー投影だけを試す seam と、実 service/SQL の回帰を明確に分離する。"""

    service = FakeCompositionService()
    application(client).state.composition_service = service
    writer = AsyncMock()
    monkeypatch.setattr(service, f"{operation}_module", writer)
    return writer


def connect(client: TestClient, session: CompositionSession) -> None:
    """入口だけ共有 fake とし、原会話を判定する事業 service/repository は差し替えない。"""

    app = application(client)
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    auth.actor = session.access.actor
    auth.session_token = session.access.session_token
    auth.csrf_token = session.access.csrf_token
    client.cookies.set(app.state.settings.auth_session_cookie_name, auth.session_token)
    client.headers["X-CSRF-Token"] = auth.csrf_token
    app.state.composition_service = session.service()


def request_session(client: TestClient, session: CompositionSession) -> ModuleHttpResponse:
    """合成保存行の精確 Project/組合/版を実公開 route へ渡す。"""

    return request_operation(
        client,
        session.operation,
        project_id=session.project.id,
        module_id=session.module_id,
        version_ids=session.version_ids,
    )


@pytest.mark.parametrize("operation", OPERATIONS)
def test_module_http_forwards_original_credentials_without_exposing_them(
    client: TestClient, operation: Operation
) -> None:
    """公開作成者 field を増やさず、元 cookie/CSRF/actor/request ID を内部へ渡す。"""

    app = application(client)
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    client.cookies.set(app.state.settings.auth_session_cookie_name, auth.session_token)
    service = FakeCompositionService()
    app.state.composition_service = service
    response = request_operation(client, operation)
    assert response.status_code == {"create": 201, "update": 200, "delete": 204}[operation]
    assert len(service.accesses) == 1
    access = service.accesses[0]
    assert access.actor == auth.actor
    assert access.session_token == auth.session_token
    assert access.csrf_token == auth.csrf_token
    assert str(access.request_id) == response.headers["x-request-id"]
    assert access.session_token not in response.text
    assert access.csrf_token not in response.text
    if operation == "delete":
        assert response.content == b""
    else:
        assert "created_by" not in response.json()
        assert "access" not in response.json()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "error_type,status,code",
    [
        (UnauthorizedSessionError, 401, "authentication_required"),
        (CsrfRejectedError, 403, "csrf_rejected"),
        (UserAdministrationDeniedError, 403, "administrator_required"),
        (ProjectNotFoundError, 404, "project_not_found"),
        (ProjectArchivedError, 409, "project_archived"),
    ],
)
def test_module_http_maps_every_business_qualification_failure_statically(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    error_type: type[Exception],
    status: int,
    code: str,
) -> None:
    """入口通過後の資格拒否も共有 Problem に写し、内部例外の詳細を公開しない。"""

    writer = install_writer(client, monkeypatch, operation)
    writer.side_effect = error_type(_PRIVATE_MARKER)
    response = request_operation(client, operation)
    assert_problem(response, status, code)
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(response.json())
    assert _PRIVATE_MARKER not in response.text
    writer.assert_awaited_once()


@pytest.mark.parametrize(
    "operation,error_type,status,code",
    [
        ("create", ModuleSkillInvalidError, 422, "module_rejected"),
        ("update", ModuleSkillInvalidError, 422, "module_rejected"),
        ("create", ModuleValidationError, 422, "module_rejected"),
        ("update", ModuleValidationError, 422, "module_rejected"),
        ("update", ModuleNotFoundError, 404, "module_not_found"),
        ("delete", ModuleNotFoundError, 404, "module_not_found"),
    ],
)
def test_module_http_preserves_codes_but_never_exposes_private_rejection_details(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    error_type: type[Exception],
    status: int,
    code: str,
) -> None:
    """異組織版や組合の診断値を漏らさず、従来の 404/422 code を維持する。"""

    writer = install_writer(client, monkeypatch, operation)
    writer.side_effect = error_type(_PRIVATE_MARKER)
    response = request_operation(client, operation)
    assert_problem(response, status, code)
    assert response.json()["detail"] == (
        "The requested module resource was not found."
        if status == 404
        else "The module name or skill bindings were rejected."
    )
    assert _PRIVATE_MARKER not in response.text
    writer.assert_awaited_once()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["connection", "timeout", "database", "unexpected"])
def test_module_http_never_retries_unknown_write_outcomes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    operation: Operation,
    failure: str,
) -> None:
    """未知の commit 結果を確定拒否へ偽装せず、同じ管理要求を再実行しない。"""

    writer = install_writer(client, monkeypatch, operation)
    error: Exception
    if failure == "connection":
        error = ConnectionError("Synthetic connection failure")
    elif failure == "timeout":
        error = TimeoutError("Synthetic timeout")
    elif failure == "database":
        error = OperationalError("synthetic statement", None, RuntimeError("synthetic DB"))
    else:
        error = RuntimeError("Synthetic unexpected failure")
    writer.side_effect = error
    with pytest.raises(type(error)) as observed:
        request_operation(client, operation)
    assert observed.value is error
    writer.assert_awaited_once()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_module_http_declares_actual_problems_without_new_cache_guarantees(
    client: TestClient, operation: Operation
) -> None:
    """実際の 401/403/404/409/422 と空 204 を宣言し、no-store は追加しない。"""

    path = "/api/v1/projects/{project_id}/modules"
    if operation != "create":
        path += "/{module_id}"
    method = {"create": "post", "update": "put", "delete": "delete"}[operation]
    declaration = application(client).openapi()["paths"][path][method]
    for status in ("401", "403", "404", "409", "422"):
        response = declaration["responses"][status]
        assert response["content"] == {
            "application/problem+json": {"schema": PROBLEM_DETAILS_SCHEMA}
        }
        assert "Cache-Control" not in response.get("headers", {})
    assert "auth" not in declaration["tags"]
    if operation == "delete":
        assert "content" not in declaration["responses"]["204"]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_module_http_missing_csrf_is_the_declared_validation_problem(
    client: TestClient, operation: Operation
) -> None:
    """既存の必須 header 欠落を 422 のまま保ち、三操作とも業務保存しない。"""

    service = FakeCompositionService()
    application(client).state.composition_service = service
    del client.headers["X-CSRF-Token"]
    response = request_operation(client, operation)
    assert_problem(response, 422, "validation_error")
    Draft202012Validator(PROBLEM_DETAILS_SCHEMA).validate(response.json())
    assert service.accesses == []


@pytest.mark.parametrize("operation", OPERATIONS)
def test_module_http_commits_actual_service_with_locked_actor_and_original_skill_content(
    client: TestClient, operation: Operation
) -> None:
    """実 SQL 判定を経た作成/更新/削除は表示順と元 Skill 内容を保持する。"""

    session = CompositionSession(operation)
    connect(client, session)
    original_manifest = deepcopy(session.manifest.manifest_json)
    original_checksum = session.manifest.checksum
    response = request_session(client, session)
    assert response.status_code == {"create": 201, "update": 200, "delete": 204}[operation]
    assert session.transactions == session.commits == 1
    assert session.rollbacks == 0
    assert session.manifest.manifest_json == original_manifest
    assert session.manifest.checksum == original_checksum
    assert session.access.session_token not in response.text
    assert session.access.csrf_token not in response.text
    if operation == "delete":
        assert response.content == b""
        assert session.enablements == []
        assert session.compositions == []
        return
    payload = response.json()
    assert payload["project_id"] == str(session.project.id)
    assert payload["name"] == "Synthetic module"
    assert payload["description"] == "Synthetic module description"
    ordered_versions = list(dict.fromkeys(session.version_ids))
    assert [item["skill_version_id"] for item in payload["skills"]] == [
        str(value) for value in ordered_versions
    ]
    assert [item["sort_order"] for item in payload["skills"]] == list(range(3))
    assert [item.skill_version_id for item in session.items] == ordered_versions
    assert session.enablements[0].enabled_by == session.user.id


def test_module_http_cannot_choose_a_different_creator_through_extra_json(
    client: TestClient,
) -> None:
    """既存の extra 入力処理を変更せず、公開されない作成者は必ず locked actor から取る。"""

    session = CompositionSession("create")
    connect(client, session)
    other_actor = uuid4()
    response = client.post(
        f"/api/v1/projects/{session.project.id}/modules",
        json={
            "name": "Synthetic module",
            "skill_version_ids": [str(session.version.id)],
            "created_by": str(other_actor),
        },
    )
    assert response.status_code == 201
    assert len(session.enablements) == 1
    assert session.enablements[0].enabled_by == session.user.id
    assert session.enablements[0].enabled_by != other_actor
    assert "created_by" not in response.json()


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize(
    "failure", ["foreign-skill", "foreign-source", "deprecated", "disabled", "missing-binding"]
)
def test_module_http_uses_current_exact_skill_binding_without_leaking_rejection_cause(
    client: TestClient, operation: Operation, failure: str
) -> None:
    """組織/状態/启用の各実 SQL 反例を同一 422 に畳み、旧組合内容も変更しない。"""

    session = CompositionSession(operation)
    connect(client, session)
    if failure == "foreign-skill":
        session.skill.organization_id = uuid4()
    elif failure == "foreign-source":
        session.source.organization_id = uuid4()
    elif failure == "deprecated":
        session.version.status = "DEPRECATED"
    elif failure == "disabled":
        session.bindings[0].disabled_at = datetime.now(UTC)
    else:
        session.bindings.clear()
    before = session.frozen_values()
    response = request_session(client, session)
    assert_problem(response, 422, "module_rejected")
    assert response.json()["detail"] == "The module name or skill bindings were rejected."
    assert session.frozen_values() == before
    assert session.mutations == []
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("failure", ["missing", "foreign"])
def test_module_http_real_module_scope_is_static_not_found(
    client: TestClient, operation: Operation, failure: str
) -> None:
    """他組織/不存在の組合に対する更新と削除は、同じ静的 404 で元資産を保持する。"""

    session = CompositionSession(operation)
    connect(client, session)
    if failure == "missing":
        session.compositions.clear()
    else:
        assert session.composition is not None
        session.composition.organization_id = uuid4()
    before = session.frozen_values()
    response = request_session(client, session)
    assert_problem(response, 404, "module_not_found")
    assert response.json()["detail"] == "The requested module resource was not found."
    assert session.frozen_values() == before
    assert session.mutations == []


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("revoked", 401, "authentication_required"),
        ("disabled", 401, "authentication_required"),
        ("csrf", 403, "csrf_rejected"),
        ("role", 403, "administrator_required"),
        ("missing-project", 404, "project_not_found"),
        ("foreign-project", 404, "project_not_found"),
        ("archived", 409, "project_archived"),
    ],
)
def test_module_http_rechecks_saved_authority_after_entry_admission(
    client: TestClient, operation: Operation, failure: str, status: int, code: str
) -> None:
    """入口が ADMIN/ACTIVE を返しても、実保存資格の失効は全変更と一緒に拒否する。"""

    session = CompositionSession(operation)
    connect(client, session)
    if failure == "revoked":
        session.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "disabled":
        session.user.status = "DISABLED"
    elif failure == "role":
        session.user.system_role = "USER"
        session.auth_session.system_role_at_login = "USER"
    elif failure == "csrf":
        auth = application(client).state.auth_service
        assert isinstance(auth, FakeAuthService)
        auth.csrf_token = "synthetic-other-session-csrf"
        client.headers["X-CSRF-Token"] = auth.csrf_token
    elif failure == "missing-project":
        session.project_present = False
    elif failure == "foreign-project":
        session.project.organization_id = uuid4()
    else:
        session.project.status = "ARCHIVED"
    before = session.frozen_values()
    response = request_session(client, session)
    assert_problem(response, status, code)
    assert session.events == ["begin", "rollback", "close"]
    assert session.frozen_values() == before
    assert session.mutations == []
    assert session.access.session_token not in response.text
    assert session.access.csrf_token not in response.text
    assert str(session.project.id) not in response.json()["detail"]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_module_http_rejects_final_flush_revocation_and_rolls_back_all_assets(
    client: TestClient, operation: Operation
) -> None:
    """repository の一回目と service の最終 flush を区別し、最終失効でも成功を返さない。"""

    session = CompositionSession(operation)
    connect(client, session)
    before = session.frozen_values()

    def revoke_after_final_flush(point: str) -> None:
        """本番 transaction 内の最後の待機後に、元 session の持続失効を合成する。"""

        if point == "flush:2":
            session.auth_session.revoked_at = datetime.now(UTC)

    session.on_step = revoke_after_final_flush
    response = request_session(client, session)
    assert_problem(response, 401, "authentication_required")
    assert session.visits["flush"] == 2
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.frozen_values() == before
    assert session.auth_session.revoked_at is not None


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["origin", "csrf", "session", "role"])
def test_module_http_preserves_entry_guards_before_opening_business_transaction(
    client: TestClient, operation: Operation, failure: str
) -> None:
    """元の Origin/CSRF/会話/ADMIN 門禁を保ち、失敗した入口から業務へ進まない。"""

    session = CompositionSession(operation)
    connect(client, session)
    auth = application(client).state.auth_service
    assert isinstance(auth, FakeAuthService)
    if failure == "origin":
        client.headers["Origin"] = "https://synthetic-other.invalid"
    elif failure == "csrf":
        client.headers["X-CSRF-Token"] = "synthetic-wrong-csrf"
    elif failure == "session":
        auth.unauthorized = True
    else:
        auth.actor = replace(auth.actor, system_role="USER")
    response = request_session(client, session)
    if failure == "session":
        assert_problem(response, 401, "authentication_required")
    elif failure == "role":
        assert_problem(response, 403, "administrator_required")
    else:
        assert_problem(response, 403, "csrf_rejected")
    assert session.transactions == 0
    assert session.events == []


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("outcome", ["not-committed", "committed"])
def test_module_http_unknown_actual_commit_does_not_automatically_retry(
    client: TestClient, operation: Operation, outcome: str
) -> None:
    """実 service の commit 応答喪失では結果を推測せず、元要求を一回だけ処理する。"""

    session = CompositionSession(operation)
    connect(client, session)
    before = session.frozen_values()
    session.commit_outcome = outcome
    with pytest.raises(ConnectionError) as observed:
        request_session(client, session)
    assert observed.value is session.commit_error
    assert session.transactions == 1
    assert session.commits == (1 if outcome == "committed" else 0)
    if outcome == "not-committed":
        assert session.frozen_values() == before
    else:
        assert session.frozen_values() != before
