"""実 DRAFT HTTP/service/repository の固定回応と静的拒否を外部接続なしで検証する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import pytest
from fakes import FakeAuthService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.api.routes.skills import _interpretation_not_ready
from projectmind.core.hashing import canonical_json, sha256_hex
from tests.skills.test_skill_draft_validation import DraftSession
from tests.skills.test_skill_publication_validation import PublicationSession


def connect(client: TestClient, session: PublicationSession) -> str:
    """共有認証 fake の actor と実 Service の保存 organization を一致させる。"""
    assert isinstance(client.app, FastAPI)
    auth = client.app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    auth.actor = replace(
        auth.actor, organization_id=session.organization_id, user_id=session.publisher
    )
    auth.session_token = session.access.session_token
    auth.csrf_token = session.access.csrf_token
    settings = client.app.state.settings
    client.cookies.set(settings.auth_session_cookie_name, session.access.session_token)
    client.headers["X-CSRF-Token"] = session.access.csrf_token
    client.app.state.skill_service = session.service()
    return f"/api/v1/skill-interpretations/{session.interpretation.id}/draft"


class HttpResponse(Protocol):
    """HTTP client の内部 class 名ではなく、公開応答の検証に必要な構造を表す。"""

    @property
    def status_code(self) -> int:
        """実 HTTP status を返す。"""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """HTTP header を返す。"""
        ...

    def json(self) -> Any:
        """公開 JSON を返し、各 test でその契約を検証する。"""
        ...


def assert_problem(response: HttpResponse, status: int, code: str) -> None:
    """Problem の公開型と共有 request ID を実 HTTP response 上で検査する。"""
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["x-request-id"]
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.json()["code"] == code


def test_http_draft_and_replay_keep_original_manifest_and_version_schema(
    client: TestClient,
) -> None:
    """ADMIN の実入口で同じ候補を再送しても保存版/原 hash を増やさない。"""
    session = DraftSession()
    path = connect(client, session)
    original = deepcopy(session.interpretation.manifest_draft_json)
    first = client.post(path)
    replay = client.post(path)
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    payload = first.json()
    assert payload["status"] == "DRAFT"
    assert payload["gate_passed"] is True
    assert payload["manifest_checksum"] == "sha256:" + sha256_hex(
        canonical_json(payload["manifest"])
    )
    assert session.interpretation.manifest_draft_json == original
    assert len(session.added) == 3
    schema_path = Path(__file__).resolve().parents[3] / "contracts/skills/version/v1.schema.json"
    Draft202012Validator(
        json.loads(schema_path.read_text()), format_checker=FormatChecker()
    ).validate(payload)
    assert first.headers["x-request-id"]
    assert replay.headers["x-request-id"]


@pytest.mark.parametrize("status", ["ANALYZING", "FAILED", "SUPERSEDED"])
def test_http_draft_rejects_non_ready_interpretation_as_static_409(
    client: TestClient,
    status: str,
) -> None:
    """保存候補の未準備を 500 ではなく既存の静的 409 に変換する。"""
    session = DraftSession()
    session.interpretation.status = status
    before = session.frozen_values()
    response = client.post(connect(client, session))
    assert_problem(response, 409, "skill_interpretation_not_ready")
    assert response.json()["detail"] == "Skill interpretation is not ready for this operation"
    assert str(session.interpretation.id) not in response.json()["detail"]
    assert session.frozen_values() == before
    assert session.added == []


def test_http_parser_cannot_claim_model_blueprint(client: TestClient) -> None:
    """新しい non-model 門禁も実 API の同じ静的 409 を通る。"""
    session = DraftSession()
    session.interpretation.origin = "deterministic_parser"
    before = session.frozen_values()
    response = client.post(connect(client, session))
    assert_problem(response, 409, "skill_interpretation_not_ready")
    assert session.frozen_values() == before
    assert session.added == []


@pytest.mark.parametrize("case", ["manifest", "identity", "skill-key"])
def test_http_draft_without_object_identity_is_refused_before_storage(
    client: TestClient,
    case: str,
) -> None:
    """識別不能な保存候補は補造せず、元 aggregate を変えない静的 409 にする。"""
    session = DraftSession()
    marker = "Private malformed candidate must not appear in the public problem"
    manifest = session.interpretation.manifest_draft_json
    if case == "manifest":
        # JSON 列に保存された型破損を再現し、通常の DTO 型を成功条件に代用しない。
        session.interpretation.manifest_draft_json = [marker]  # type: ignore[assignment]
    elif case == "identity":
        manifest["identity"] = [marker]
    else:
        manifest["identity"]["skill_key"] = [marker]
    before = session.frozen_values()
    response = client.post(connect(client, session))
    assert_problem(response, 409, "skill_interpretation_not_ready")
    assert response.json()["detail"] == "Skill interpretation is not ready for this operation"
    assert marker not in response.text
    assert session.frozen_values() == before
    assert session.added == []
    assert session.flushes == []


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("foreign-skill", 404, "skill_interpretation_not_found"),
        ("version-source", 409, "skill_interpretation_not_ready"),
        ("manifest-interpretation", 409, "skill_interpretation_not_ready"),
        ("manifest-version", 409, "skill_interpretation_not_ready"),
    ],
)
def test_http_replay_rejects_wrong_saved_aggregate_without_changing_receipt(
    client: TestClient,
    case: str,
    status: int,
    code: str,
) -> None:
    """同じ解釈 ID を用いても、別組織や不正 FK の既存 DRAFT を返さない。"""
    session = DraftSession()
    path = connect(client, session)
    first = client.post(path)
    assert first.status_code == 201
    receipt = first.json()
    if case == "foreign-skill":
        session.skill.organization_id = uuid4()
    elif case == "version-source":
        session.version.skill_source_id = uuid4()
    elif case == "manifest-interpretation":
        session.manifest.interpretation_id = uuid4()
    else:
        session.manifest.skill_version_id = uuid4()
    before = session.frozen_values()
    replay = client.post(path)
    assert_problem(replay, status, code)
    assert session.frozen_values() == before
    assert first.json() == receipt
    assert len(session.added) == 3


def test_http_damaged_source_can_be_reviewed_only_as_failed_draft(client: TestClient) -> None:
    """source の破損は審査可能な DRAFT finding に留め、source 本文を公開エラーに写さない。"""
    session = DraftSession()
    marker = "Private source content must not appear in gate diagnostics"
    session.source.source_snapshot_json[0]["content"] += marker
    response = client.post(connect(client, session))
    assert response.status_code == 201
    assert response.json()["gate_passed"] is False
    assert marker not in response.text
    assert any(finding["severity"] == "error" for finding in response.json()["gate_findings"])


def test_shared_not_ready_problem_never_echoes_internal_diagnostics() -> None:
    """draft と adjust が共有する変換口で、原因例外の機密値を本文へ流さない。"""
    problem = _interpretation_not_ready(ValueError("Private source diagnostic"))
    assert problem.status == 409
    assert problem.code == "skill_interpretation_not_ready"
    assert problem.detail == "Skill interpretation is not ready for this operation"


def test_draft_openapi_declares_existing_409_problem_shape(client: TestClient) -> None:
    """実 app の声明で新しい拒否 status を既存の公開 Problem として列挙する。"""
    assert isinstance(client.app, FastAPI)
    operation = client.app.openapi()["paths"][
        "/api/v1/skill-interpretations/{interpretation_id}/draft"
    ]["post"]
    rejection = operation["responses"]["409"]
    assert "application/problem+json" in rejection["content"]


@pytest.mark.parametrize("operation", ["draft", "publish"])
@pytest.mark.parametrize(
    "failure,status,code",
    [
        ("revoked", 401, "authentication_required"),
        ("csrf", 403, "csrf_rejected"),
        ("role", 403, "administrator_required"),
    ],
)
def test_skill_write_maps_real_business_transaction_authorization_rejection(
    client: TestClient, operation: str, failure: str, status: int, code: str
) -> None:
    """入口 ADMIN fake が通過しても、実 service の原資格拒否を共有 Problem へ写す。"""
    session = DraftSession() if operation == "draft" else PublicationSession()
    path = connect(client, session)
    if operation == "publish":
        path = f"/api/v1/skill-versions/{session.version.id}/publish"
    if failure == "revoked":
        session.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "role":
        session.user.system_role = "USER"
        session.auth_session.system_role_at_login = "USER"
    else:
        assert isinstance(client.app, FastAPI)
        auth = client.app.state.auth_service
        assert isinstance(auth, FakeAuthService)
        # 入口だけを通過させ、共有 business authorizer には誤った原 header を届ける。
        auth.csrf_token = "synthetic-other-session-csrf"
        client.headers["X-CSRF-Token"] = auth.csrf_token
    before = session.frozen_values()
    response = client.post(path, json={} if operation == "publish" else None)
    assert_problem(response, status, code)
    assert session.events == ["begin", "rollback", "close"]
    assert session.frozen_values() == before
    assert session.flush_calls == 0
    assert session.access.session_token not in response.text
    assert session.access.csrf_token not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/skill-interpretations/{interpretation_id}/draft",
        "/api/v1/skill-versions/{skill_version_id}/publish",
    ],
)
def test_skill_write_openapi_declares_business_401_and_403_problems(
    client: TestClient, path: str
) -> None:
    """実際に捕捉する原資格の失敗を宣言し、未提供の no-store header を補造しない。"""
    assert isinstance(client.app, FastAPI)
    responses = client.app.openapi()["paths"][path]["post"]["responses"]
    for status in ("401", "403"):
        assert "application/problem+json" in responses[status]["content"]
        assert "Cache-Control" not in responses[status].get("headers", {})
