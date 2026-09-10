"""四管理 HTTP を実資格/Service/Repository に接続し、合成 SQL の範囲で検証する。

入口の認証と Project 読み取りだけは共有 fake を使う。事業 transaction の原資格判定は
差し替えず、DB 接続/実 lock 競争の検証と取り違えない。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.db.models import RuntimeManifest
from tests.api.fakes import FakeAuthService
from tests.api.test_skill_draft_api import HttpResponse, assert_problem, connect
from tests.skills.skill_lifecycle_harness import (
    OPERATIONS,
    REFERENCE_MODELS,
    LifecycleSession,
    Operation,
)


class LifecycleHttpResponse(HttpResponse, Protocol):
    """既存 Problem 構造に加え、空 204 と非漏洩を検査する公開 response 境界。"""

    @property
    def content(self) -> bytes:
        """HTTP の実 byte 列を返す。"""
        ...

    @property
    def text(self) -> str:
        """応答本文への私有資格混入を確認する。"""
        ...


def request_operation(client: TestClient, session: LifecycleSession) -> LifecycleHttpResponse:
    """同じ原 cookie/header を四つの既存公開 endpoint へそのまま渡す。"""
    version_path = f"/api/v1/skill-versions/{session.version.id}"
    project_path = f"/api/v1/projects/{session.project.id}/skill-versions/{session.version.id}"
    if session.operation == "deprecate":
        return client.post(version_path + "/deprecate")
    if session.operation == "delete":
        return client.delete(version_path)
    if session.operation == "enable":
        return client.put(project_path)
    return client.delete(project_path)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_lifecycle_http_keeps_original_receipts_and_frozen_manifest(
    client: TestClient, operation: Operation
) -> None:
    """正常四操作/合法重送の HTTP を既存 schema と照合し、原 Manifest を書き換えない。"""
    session = LifecycleSession(operation)
    connect(client, session)
    manifest = deepcopy(session.manifest.manifest_json)
    checksum = session.manifest.checksum
    first = request_operation(client, session)
    replay = request_operation(client, session)
    assert session.manifest.manifest_json == manifest
    assert session.manifest.checksum == checksum
    assert session.access.session_token not in first.text
    assert session.access.csrf_token not in first.text
    assert first.headers["x-request-id"]
    if operation == "delete":
        assert first.status_code == 204
        assert first.content == b""
        assert_problem(replay, 404, "skill_version_not_found")
        assert session.bindings == []
        assert session.manifest not in session.rows
        assert session.version not in session.rows
        assert session.source in session.rows
        assert session.interpretation in session.rows
        assert session.commits == 1
        return
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    payload = first.json()
    schema_name = "version" if operation == "deprecate" else "project-enablement"
    schema_path = (
        Path(__file__).resolve().parents[3] / f"contracts/skills/{schema_name}/v1.schema.json"
    )
    Draft202012Validator(
        json.loads(schema_path.read_text()), format_checker=FormatChecker()
    ).validate(payload)
    if operation == "deprecate":
        assert payload["status"] == "DEPRECATED"
        assert payload["skill_version_id"] == str(session.version.id)
    else:
        assert payload["project_id"] == str(session.project.id)
        assert payload["skill_version"]["skill_version_id"] == str(session.version.id)
        assert payload["enabled_by"] == str(session.user.id)
        assert (payload["disabled_at"] is not None) is (operation == "disable")
        assert len(session.bindings) == 1
    assert session.commits == 2


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("revoked", 401, "authentication_required"),
        ("csrf", 403, "csrf_rejected"),
        ("role", 403, "administrator_required"),
    ],
)
def test_lifecycle_http_maps_original_business_qualification_failure(
    client: TestClient, operation: Operation, failure: str, status: int, code: str
) -> None:
    """入口 fake の ADMIN 判定に頼らず、保存原会話の拒否を実 HTTP Problem に写す。"""
    session = LifecycleSession(operation)
    connect(client, session)
    if failure == "revoked":
        session.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "role":
        session.user.system_role = "USER"
        session.auth_session.system_role_at_login = "USER"
    else:
        assert isinstance(client.app, FastAPI)
        auth = client.app.state.auth_service
        assert isinstance(auth, FakeAuthService)
        # 入口だけ通る誤 header を使い、実 business authorizer は元 hash を再照合する。
        auth.csrf_token = "synthetic-lifecycle-other-csrf"
        client.headers["X-CSRF-Token"] = auth.csrf_token
    before = session.frozen_values()
    response = request_operation(client, session)
    assert_problem(response, status, code)
    assert session.events == ["begin", "rollback", "close"]
    assert session.frozen_values() == before
    assert session.flush_calls == 0
    assert session.access.session_token not in response.text
    assert session.access.csrf_token not in response.text


@pytest.mark.parametrize("operation", OPERATIONS)
def test_lifecycle_http_does_not_return_success_after_final_flush_revocation(
    client: TestClient, operation: Operation
) -> None:
    """資格の最終検査が拒否した場合、既に変えた資産も rollback して 401 を返す。"""
    session = LifecycleSession(operation)
    connect(client, session)
    before = session.frozen_values()

    def revoke_after_flush(point: str) -> None:
        """本番 flush 完了の待機境界にだけ、外部に保存された会話失効を注入する。"""
        if point == "flush:1":
            session.auth_session.revoked_at = datetime.now(UTC)

    session.on_step = revoke_after_flush
    response = request_operation(client, session)
    assert_problem(response, 401, "authentication_required")
    assert session.flush_calls == 1
    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.frozen_values() == before
    assert session.auth_session.revoked_at is not None


@pytest.mark.parametrize("operation", ["enable", "disable"])
@pytest.mark.parametrize("failure", ["missing", "foreign", "archived"])
def test_project_lifecycle_http_rechecks_project_inside_business_transaction(
    client: TestClient, operation: Operation, failure: str
) -> None:
    """入口の ACTIVE Project 応答後でも、実 transaction の別組織/不存在/帰档を拒否する。"""
    session = LifecycleSession(operation)
    connect(client, session)
    if failure == "missing":
        session.project_present = False
    elif failure == "foreign":
        session.project.organization_id = uuid4()
    else:
        session.project.status = "ARCHIVED"
    before = session.frozen_values()
    response = request_operation(client, session)
    status, code = (
        (409, "project_archived") if failure == "archived" else (404, "project_not_found")
    )
    assert_problem(response, status, code)
    assert session.events == ["begin", "rollback", "close"]
    assert session.frozen_values() == before
    assert session.mutations == []
    assert str(session.project.id) not in response.json()["detail"]


@pytest.mark.parametrize(
    "operation,code",
    [
        ("deprecate", "skill_version_transition_rejected"),
        ("delete", "skill_version_delete_blocked"),
        ("enable", "project_skill_version_rejected"),
        ("disable", "project_skill_version_rejected"),
    ],
)
def test_lifecycle_http_missing_manifest_is_static_conflict(
    client: TestClient, operation: Operation, code: str
) -> None:
    """壊れた保存 aggregate を 500 や成功にせず、操作既存の静的 409 にする。"""
    session = LifecycleSession(operation)
    connect(client, session)
    session.missing.add(RuntimeManifest)
    before = session.frozen_values()
    response = request_operation(client, session)
    assert_problem(response, 409, code)
    assert response.json()["detail"] == "SkillVersion has no frozen RuntimeManifest"
    assert session.frozen_values() == before
    assert session.mutations == []


@pytest.mark.parametrize("model_name", [model.__name__ for model in REFERENCE_MODELS])
def test_delete_http_preserves_each_saved_execution_or_configuration_reference(
    client: TestClient, model_name: str
) -> None:
    """六種類の監査/設定参照を実 SQL predicate で照合し、削除も可視性変更もしない。"""
    session = LifecycleSession("delete")
    connect(client, session)
    model = next(item for item in REFERENCE_MODELS if item.__name__ == model_name)
    session.references[model].add(session.version.id)
    before = session.frozen_values()
    response = request_operation(client, session)
    assert_problem(response, 409, "skill_version_delete_blocked")
    assert response.json()["detail"] == (
        "SkillVersion is still referenced by execution or configuration records"
    )
    assert session.frozen_values() == before
    assert session.mutations == []


def test_enable_http_cannot_erase_original_disable_audit(client: TestClient) -> None:
    """正常有効化→停用の後、同じ PUT で原 binding の disabled_at を消せない。"""
    session = LifecycleSession("enable")
    connect(client, session)
    enabled = request_operation(client, session)
    assert enabled.status_code == 200
    session.operation = "disable"
    disabled = request_operation(client, session)
    assert disabled.status_code == 200
    before = session.frozen_values()
    session.operation = "enable"
    rejected = request_operation(client, session)
    assert_problem(rejected, 409, "project_skill_version_rejected")
    assert session.frozen_values() == before
    assert session.binding.disabled_at is not None
    assert enabled.json()["enabled_at"] == disabled.json()["enabled_at"]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["origin", "csrf", "missing-csrf", "session", "role"])
def test_lifecycle_http_keeps_entry_guards_before_business_work(
    client: TestClient, operation: Operation, failure: str
) -> None:
    """既存 Origin/CSRF/ADMIN 拒否を維持し、業務 transaction を起動しない。"""
    session = LifecycleSession(operation)
    connect(client, session)
    assert isinstance(client.app, FastAPI)
    auth = client.app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    if failure == "origin":
        client.headers["Origin"] = "https://synthetic-other.invalid"
    elif failure == "csrf":
        client.headers["X-CSRF-Token"] = "synthetic-wrong-csrf"
    elif failure == "missing-csrf":
        del client.headers["X-CSRF-Token"]
    elif failure == "session":
        auth.unauthorized = True
    else:
        auth.actor = replace(auth.actor, system_role="USER")
    before = session.frozen_values()
    response = request_operation(client, session)
    if failure == "missing-csrf":
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")
    elif failure == "session":
        assert_problem(response, 401, "authentication_required")
    elif failure == "role":
        assert_problem(response, 403, "administrator_required")
    else:
        assert_problem(response, 403, "csrf_rejected")
    assert session.events == []
    assert session.frozen_values() == before


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/v1/skill-versions/{skill_version_id}/deprecate", "post"),
        ("/api/v1/skill-versions/{skill_version_id}", "delete"),
        ("/api/v1/projects/{project_id}/skill-versions/{skill_version_id}", "put"),
        ("/api/v1/projects/{project_id}/skill-versions/{skill_version_id}", "delete"),
    ],
)
def test_lifecycle_http_openapi_matches_authorization_and_project_conflicts(
    client: TestClient, path: str, method: str
) -> None:
    """捕捉する Problem を宣言するが、未提供の no-store header を付け加えない。"""
    assert isinstance(client.app, FastAPI)
    responses = client.app.openapi()["paths"][path][method]["responses"]
    for status in ("401", "403", "404", "409"):
        assert "application/problem+json" in responses[status]["content"]
        assert "Cache-Control" not in responses[status].get("headers", {})
