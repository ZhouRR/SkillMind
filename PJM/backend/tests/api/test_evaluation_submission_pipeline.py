"""実 Evaluation HTTP/service/repository を SQL fake へ接続し、元の受付記録と拒否を検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fakes import FakeAuthService
from fastapi import FastAPI
from fastapi.testclient import TestClient

from projectmind.auth.domain import generate_session_credentials
from projectmind.core.settings import Settings
from tests.evaluations.submission_harness import EvaluationDatabase


def _connect(client: TestClient, database: EvaluationDatabase) -> str:
    """入口だけの認証 fake と実業務再認証へ同一の合成資格を渡す。"""

    assert isinstance(client.app, FastAPI)
    auth = client.app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    auth.actor = database.access.actor
    auth.session_token = database.access.session_token
    auth.csrf_token = database.access.csrf_token
    settings = client.app.state.settings
    assert isinstance(settings, Settings)
    client.cookies.set(settings.auth_session_cookie_name, database.access.session_token)
    client.headers["X-CSRF-Token"] = database.access.csrf_token
    client.app.state.evaluation_service = database.evaluation_service
    return f"/api/v1/projects/{database.project.id}/runs/{database.run.id}"


def _body(database: EvaluationDatabase, *, key: UUID | None = None) -> dict[str, Any]:
    """内部 command の余分な actor/Project field を公開要求へ混ぜない。"""

    command = database.command
    return {
        "submission_key": str(key if key is not None else database.submission_key),
        "result_id": str(database.result.id),
        "rating": command.rating,
        "verdict": command.verdict.value,
        "comment": command.comment,
        "revisions": [
            {
                "pointer": revision.pointer,
                "suggested_value": deepcopy(revision.suggested_value),
                "reason": revision.reason,
            }
            for revision in command.revisions
        ],
    }


def _query(base: str, body: dict[str, Any]) -> str:
    """確認 GET は原 Result/key のみで構成し、評価本文を URL へ保存しない。"""

    return (
        f"{base}/evaluation-submissions/{body['submission_key']}"
        f"?result_id={body['result_id']}"
    )


def test_original_http_submission_replay_and_lookup_share_one_saved_evaluation(
    client: TestClient,
) -> None:
    """保存受付が POST/GET を通じて同一で、Result と修正案の原値を変更しない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    original = deepcopy(database.result.data_json)
    first = client.post(f"{base}/evaluation-submissions", json=body)
    replay = client.post(f"{base}/evaluation-submissions", json=body)
    lookup = client.get(_query(base, body))
    assert first.status_code == 201
    assert replay.status_code == lookup.status_code == 200
    assert first.json() == replay.json() == lookup.json()
    assert len(database.evaluations) == 1
    assert database.result.data_json == original
    assert set(first.json()) == {"project_id", "run_id", "submission_key", "evaluation"}
    assert set(first.json()["evaluation"]) == {
        "evaluation_id", "result_id", "run_id", "user_id", "rating", "verdict",
        "comment", "revisions", "created_at",
    }
    for response in (first, replay, lookup):
        assert response.headers["cache-control"] == "no-store"
        assert "request_hash" not in response.json()["evaluation"]


@pytest.mark.parametrize("persisted", [False, True])
def test_http_commit_unknown_is_resolved_only_by_original_record_or_same_key_resubmit(
    client: TestClient, persisted: bool,
) -> None:
    """commit 応答喪失の前後とも API は成功を返さず、GET は初回書込を発行しない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    database.commit_unknown = persisted
    first = client.post(f"{base}/evaluation-submissions", json=body)
    assert first.status_code == 503
    assert len(database.evaluations) == int(persisted)
    database.commit_unknown = None
    lookup = client.get(_query(base, body))
    assert lookup.status_code == (200 if persisted else 404)
    if not persisted:
        assert lookup.json()["code"] == "evaluation_submission_not_found"
    assert len(database.evaluations) == int(persisted)
    retry = client.post(f"{base}/evaluation-submissions", json=body)
    assert retry.status_code == (200 if persisted else 201)
    assert len(database.evaluations) == 1
    if persisted:
        assert retry.json() == lookup.json()
    assert client.get(_query(base, body)).json() == retry.json()


def test_same_key_changed_content_is_not_another_evaluation(client: TestClient) -> None:
    """元の comment を変えた再送は衝突し、明示的新規だけが別評価を追加する。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    original = _body(database)
    first = client.post(f"{base}/evaluation-submissions", json=original)
    assert first.status_code == 201
    changed = {**original, "comment": "A separate human judgment"}
    conflict = client.post(f"{base}/evaluation-submissions", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "evaluation_submission_conflict"
    assert len(database.evaluations) == 1
    assert client.get(_query(base, original)).json() == first.json()
    changed["submission_key"] = str(uuid4())
    second = client.post(f"{base}/evaluation-submissions", json=changed)
    assert second.status_code == 201 and len(database.evaluations) == 2
    first_id = first.json()["evaluation"]["evaluation_id"]
    assert second.json()["evaluation"]["evaluation_id"] != first_id


def test_omitted_defaults_and_explicit_defaults_confirm_the_same_request(
    client: TestClient,
) -> None:
    """省略時の既定値を正規化し、field 有無だけで同一原要求を衝突にしない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    body.pop("comment")
    body.pop("revisions")
    first = client.post(f"{base}/evaluation-submissions", json=body)
    replay = client.post(
        f"{base}/evaluation-submissions", json={**body, "comment": "", "revisions": []},
    )
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json() and len(database.evaluations) == 1


@pytest.mark.parametrize("failure,status,code", [
    ("revoked", 401, "authentication_required"),
    ("removed", 404, "project_not_found"),
    ("archived", 409, "project_archived"),
])
def test_http_entry_authorization_does_not_bypass_business_transaction_access(
    client: TestClient, failure: str, status: int, code: str,
) -> None:
    """入口 fake が許可しても、実業務 transaction は撤権後の新規・再送を止める。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    first = client.post(f"{base}/evaluation-submissions", json=body)
    assert first.status_code == 201
    if failure == "revoked":
        database.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "removed":
        assert database.member is not None
        database.member.status = "REMOVED"
    else:
        database.project.status = "ARCHIVED"
    denied = client.post(f"{base}/evaluation-submissions", json=body)
    assert denied.status_code == status and denied.json()["code"] == code
    assert len(database.evaluations) == 1
    lookup = client.get(_query(base, body))
    if failure == "archived":
        assert lookup.status_code == 200 and lookup.json() == first.json()
    else:
        assert lookup.status_code == status


def test_another_current_actor_cannot_confirm_the_original_key(client: TestClient) -> None:
    """同じ Project/Result に access があっても、他人の key を自身の受付記録にしない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    assert client.post(f"{base}/evaluation-submissions", json=body).status_code == 201
    other_user = uuid4()
    database.user.id = other_user
    database.auth_session.user_id = other_user
    assert database.member is not None
    database.member.user_id = other_user
    database.access = replace(
        database.access, actor=replace(database.access.actor, user_id=other_user),
    )
    _connect(client, database)
    response = client.get(_query(base, body))
    assert response.status_code == 404
    assert response.json()["code"] == "evaluation_submission_not_found"
    assert len(database.evaluations) == 1


def test_current_new_session_can_read_original_submission_without_reviving_old_session(
    client: TestClient,
) -> None:
    """同一利用者の新会話は GET 専用確認を行え、旧 cookie を再有効化しない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    first = client.post(f"{base}/evaluation-submissions", json=body)
    assert first.status_code == 201
    old_access = database.access
    credentials = generate_session_credentials()
    new_session = deepcopy(database.auth_session)
    new_session.id = uuid4()
    new_session.token_hash = credentials.session_token_hash
    new_session.csrf_token_hash = credentials.csrf_token_hash
    database.auth_sessions.append(new_session)
    database.auth_session.revoked_at = datetime.now(UTC)
    database.access = replace(
        old_access, session_token=credentials.session_token, csrf_token=credentials.csrf_token,
    )
    _connect(client, database)
    del client.headers["X-CSRF-Token"]
    lookup = client.get(_query(base, body))
    assert lookup.status_code == 200 and lookup.json() == first.json()
    assert len(database.evaluations) == 1
    database.access = old_access
    _connect(client, database)
    rejected = client.get(_query(base, body))
    assert rejected.status_code == 401 and rejected.json()["code"] == "authentication_required"
    assert len(database.evaluations) == 1


@pytest.mark.parametrize("corruption", [
    "request_hash", "submission_key", "suggestion", "original_value",
])
def test_corrupted_saved_evaluation_never_becomes_a_confirmed_http_receipt(
    client: TestClient, corruption: str,
) -> None:
    """request hash だけでなく原 Result の値も確認し、壊れた受付記録を正常化しない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    assert body["revisions"]
    assert client.post(f"{base}/evaluation-submissions", json=body).status_code == 201
    saved = database.evaluations[0]
    if corruption == "request_hash":
        saved.request_hash = "sha256:" + "0" * 64
    elif corruption == "submission_key":
        saved.submission_key = uuid4()
        body["submission_key"] = str(saved.submission_key)
    else:
        field = "suggested_value" if corruption == "suggestion" else "original_value"
        saved.revision_json[0][field] = {"private_test_marker": "Not an original value"}
    for response in (
        client.get(_query(base, body)),
        client.post(f"{base}/evaluation-submissions", json=body),
    ):
        assert response.status_code == 503
        assert response.json()["code"] == "evaluation_record_invalid"
        assert "private_test_marker" not in response.text
    assert len(database.evaluations) == 1


def test_http_cursor_pages_cover_saved_history_and_reject_foreign_positions(
    client: TestClient,
) -> None:
    """複数 page が全評価を一度ずつ返し、cursor が別 Result の位置を持ち込まない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    identities: list[str] = []
    for _ in range(25):
        response = client.post(
            f"{base}/evaluation-submissions", json=_body(database, key=uuid4()),
        )
        assert response.status_code == 201
        identities.append(response.json()["evaluation"]["evaluation_id"])
    actual: list[str] = []
    after: str | None = None
    for _ in range(4):
        query: dict[str, str | int] = {"limit": 7}
        if after is not None:
            query["after"] = after
        page = client.get(f"{base}/evaluations/page", params=query)
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        payload = page.json()
        assert payload["result_id"] == str(database.result.id)
        actual.extend(item["evaluation_id"] for item in payload["items"])
        after = payload["next_cursor"]
    assert after is None and actual == identities
    foreign = deepcopy(database.evaluations[0])
    foreign.id, foreign.result_id = uuid4(), uuid4()
    database.evaluations.append(foreign)
    invalid = client.get(f"{base}/evaluations/page", params={"after": str(foreign.id)})
    assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_evaluation_cursor"


def test_legacy_posts_remain_append_only_and_do_not_gain_fabricated_request_keys(
    client: TestClient,
) -> None:
    """旧 client の二回の新規作成を一回に潰さず、旧履歴にも新 key を補造しない。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    original = _body(database)
    body = {
        key: value for key, value in original.items()
        if key not in {"submission_key", "result_id"}
    }
    first = client.post(f"{base}/evaluations", json=body)
    second = client.post(f"{base}/evaluations", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["evaluation_id"] != second.json()["evaluation_id"]
    assert all(
        row.submission_key is None and row.request_hash is None for row in database.evaluations
    )
    history = client.get(f"{base}/evaluations")
    assert history.status_code == 200 and history.json()["items"] == [first.json(), second.json()]
    assert client.get(_query(base, original)).status_code == 404


@pytest.mark.parametrize("operation", ["submit", "lookup", "page"])
def test_final_wait_expiry_is_mapped_to_unauthorized_without_partial_evaluation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """保存済み期限は変えず、最終 flush の待機時間を実 HTTP の 401 まで伝える。"""

    database = EvaluationDatabase()
    base = _connect(client, database)
    body = _body(database)
    initial = datetime.now(UTC)
    database.auth_session.idle_expires_at = initial + timedelta(seconds=1)

    class Clock:
        """共有資格 validator に渡る評価用例時計だけを進める。"""

        current = initial

        @staticmethod
        def now(timezone: object) -> datetime:
            """外部 clock や他領域を変更せず、指定した時刻を返す。"""

            del timezone
            return Clock.current

    def advance(step: str) -> None:
        """最終待機の後では入口時刻を再利用できないことを確かめる。"""

        if step == "flush":
            Clock.current = initial + timedelta(seconds=2)

    monkeypatch.setattr("projectmind.evaluations.service.datetime", Clock)
    if operation == "lookup":
        assert client.post(f"{base}/evaluation-submissions", json=body).status_code == 201
    database.on_step = advance
    if operation == "submit":
        response = client.post(f"{base}/evaluation-submissions", json=body)
    elif operation == "lookup":
        response = client.get(_query(base, body))
    else:
        response = client.get(f"{base}/evaluations/page")
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert len(database.evaluations) == int(operation == "lookup")
