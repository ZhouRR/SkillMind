"""Run 作成・取消・interaction・detail・history API の契約を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fakes import FakeAuthService, FakeRunService, FakeSkillService
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.auth.service import AuthenticatedActor
from projectmind.runs.domain import CreatedRun, RunStatus
from tests.documents.fakes import document_content, document_snapshot


def test_create_task_run_returns_server_snapshot_identity(client: TestClient) -> None:
    """通用 Run 作成 API が解決済み task と Location を返すことを確認する。"""

    fake = FakeRunService()
    client.app.state.run_service = fake
    project_id = uuid4()
    response = _create_task_run(client, project_id)

    assert response.status_code == 201
    assert response.json()["status"] == "QUEUED"
    assert response.headers["Idempotent-Replay"] == "false"
    assert response.headers["Location"] == (f"/projectmind/api/v1/runs/{response.json()['run_id']}")
    assert fake.received_input == {"target": "main"}
    auth = client.app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    assert fake.received_actor_id == auth.actor.user_id
    assert fake.received_actor_role == "ADMIN"
    assert fake.received_membership == "ADMIN_BYPASS"

    fetched = client.get(f"/api/v1/runs/{response.json()['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == response.json()


def _task_run_body(**overrides: object) -> dict[str, object]:
    """task-runs endpoint 用の request body を組み立てる。"""

    body: dict[str, object] = {
        "skill_version_id": str(uuid4()),
        "task_key": "review-change",
        "input": {"target": "main"},
        "sources": {"repository-source": "git"},
    }
    body.update(overrides)
    return body


def _create_task_run(client: TestClient, project_id: object) -> Any:
    """Generic task service を配線して task-runs endpoint を呼ぶ。"""

    client.app.state.skill_service = FakeSkillService()
    return client.post(
        f"/api/v1/projects/{project_id}/task-runs",
        headers={"Idempotency-Key": "request-001"},
        json=_task_run_body(),
    )


def test_create_task_run_creates_generic_run(client: TestClient) -> None:
    """通用 task-runs が resolve→create を経て server 固定 Run を返す。"""

    client.app.state.skill_service = FakeSkillService()
    run_fake = FakeRunService()
    client.app.state.run_service = run_fake
    project_id = uuid4()
    response = client.post(
        f"/api/v1/projects/{project_id}/task-runs",
        headers={"Idempotency-Key": "req-task-1"},
        json=_task_run_body(),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "QUEUED"
    assert response.headers["Idempotent-Replay"] == "false"
    assert run_fake.received_sources == {"repository-source": "git"}
    assert run_fake.received_input == {"target": "main"}
    assert run_fake.received_membership == "ADMIN_BYPASS"


def test_create_task_run_requires_valid_csrf(client: TestClient) -> None:
    """Generic task Run の unsafe request は共有 CSRF 境界で拒否する。"""

    client.app.state.skill_service = FakeSkillService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "req-task-csrf", "X-CSRF-Token": "invalid"},
        json=_task_run_body(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"


def test_active_project_user_can_create_generic_task_run(client: TestClient) -> None:
    """ACTIVE member USER の generic Run 実行に actor identity を固定する。"""

    auth = FakeAuthService()
    auth.actor = AuthenticatedActor(
        user_id=auth.actor.user_id,
        organization_id=auth.actor.organization_id,
        email=auth.actor.email,
        display_name=auth.actor.display_name,
        system_role="USER",
    )
    run_fake = FakeRunService()
    client.app.state.auth_service = auth
    client.app.state.skill_service = FakeSkillService()
    client.app.state.run_service = run_fake
    client.headers["X-CSRF-Token"] = auth.csrf_token

    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "req-task-user"},
        json=_task_run_body(),
    )

    assert response.status_code == 201
    assert run_fake.received_actor_id == auth.actor.user_id
    assert run_fake.received_membership == "ACTIVE"


def test_create_task_run_returns_404_for_unpublished_task(client: TestClient) -> None:
    """未公開/不存在の task は安定した 404 へ畳み込む。"""

    client.app.state.skill_service = FakeSkillService(published_task_missing=True)
    client.app.state.run_service = FakeRunService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "req-task-2"},
        json=_task_run_body(),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "published_task_not_found"


def test_create_task_run_returns_422_for_invalid_input(client: TestClient) -> None:
    """Task input schema 非適合の入力を 422 へ変換する。"""

    client.app.state.skill_service = FakeSkillService(task_input_invalid=True)
    client.app.state.run_service = FakeRunService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "req-task-3"},
        json=_task_run_body(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "task_input_invalid"


def test_create_task_run_returns_422_for_invalid_source_selection(client: TestClient) -> None:
    """受理されない data source 選択を 422 へ変換する。"""

    client.app.state.skill_service = FakeSkillService()
    client.app.state.run_service = FakeRunService(source_invalid=True)
    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "req-task-4"},
        json=_task_run_body(sources={"repository-source": "svn"}),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "task_source_selection_invalid"


def test_create_task_run_replay_returns_200(client: TestClient) -> None:
    """同一 request の idempotent replay が 200 と replay header を返すことを確認する。"""

    client.app.state.run_service = FakeRunService(replay=True)
    response = _create_task_run(client, uuid4())

    assert response.status_code == 200
    assert response.headers["Idempotent-Replay"] == "true"


def test_create_task_run_rejects_idempotency_conflict(client: TestClient) -> None:
    """同じ key の異なる request が安定した 409 Problem を返すことを確認する。"""

    client.app.state.run_service = FakeRunService(conflict=True)
    response = _create_task_run(client, uuid4())

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "idempotency_conflict"


def test_replay_does_not_require_task_to_still_be_published(client: TestClient) -> None:
    """有権限の原要求再送は公開停止後も旧 Run を確認でき、新しい実行は作らない。"""

    client.app.state.skill_service = FakeSkillService(published_task_missing=True)
    fake = FakeRunService(replay=True)
    client.app.state.run_service = fake
    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "original-request"},
        json=_task_run_body(),
    )

    assert response.status_code == 200
    assert response.headers["Idempotent-Replay"] == "true"
    assert fake.replay_lookups == 1


def test_replay_keeps_csrf_and_current_actor_checks(client: TestClient) -> None:
    """既存 Run を返すだけでも unsafe endpoint の認証境界は通過する。"""

    fake = FakeRunService(replay=True)
    client.app.state.run_service = fake
    response = client.post(
        f"/api/v1/projects/{uuid4()}/task-runs",
        headers={"Idempotency-Key": "original-request", "X-CSRF-Token": "invalid"},
        json=_task_run_body(),
    )
    assert response.status_code == 403
    assert fake.replay_lookups == 0


def test_task_resolution_failure_rechecks_a_just_committed_request(client: TestClient) -> None:
    """事前照会と公開版解決の間に初回が commit した場合も、その Run を確認する。"""

    project_id = uuid4()
    expected = CreatedRun(
        run_id=uuid4(),
        project_id=project_id,
        task_id=uuid4(),
        status=RunStatus.RUNNING,
        row_version=3,
        created_at=datetime.now(UTC),
        idempotent_replay=True,
    )
    fake = FakeRunService()
    fake.find_task_run_replay = AsyncMock(side_effect=[None, expected])
    client.app.state.run_service = fake
    client.app.state.skill_service = FakeSkillService(published_task_missing=True)
    response = client.post(
        f"/api/v1/projects/{project_id}/task-runs",
        headers={"Idempotency-Key": "original-request"},
        json=_task_run_body(),
    )
    assert response.status_code == 200
    assert response.json()["run_id"] == str(expected.run_id)
    assert fake.find_task_run_replay.await_count == 2
    assert (
        fake.find_task_run_replay.call_args_list[0] == fake.find_task_run_replay.call_args_list[1]
    )
    assert fake.received_input is None


def test_cancel_run_returns_terminal_snapshot_and_is_idempotent(client: TestClient) -> None:
    """取消 API が明示 field だけを返し、反復要求でも CANCELLED を維持する。"""

    fake = FakeRunService()
    client.app.state.run_service = fake
    created = _create_task_run(client, uuid4()).json()

    first = client.post(f"/api/v1/runs/{created['run_id']}/cancel")
    second = client.post(f"/api/v1/runs/{created['run_id']}/cancel")

    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "CANCELLED"
    assert first.json()["cancellation"] == "CANCELLED"


def test_get_run_detail_returns_project_scoped_result_and_evidence(client: TestClient) -> None:
    """Run detail が Result と脱敏済み ToolCall/Evidence だけを返す。"""

    fake = FakeRunService()
    client.app.state.run_service = fake
    project_id = uuid4()
    created = _create_task_run(client, project_id).json()

    response = client.get(f"/api/v1/projects/{project_id}/runs/{created['run_id']}/detail")

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["summary"] == "completed"
    assert payload["tool_calls"][0]["capability"] == "repository.read/v1"
    assert "result_json" not in payload["tool_calls"][0]
    assert payload["evidence"][0]["evidence_ref"] == "ev_repo_001"
    assert payload["segments"][0]["segment_no"] == 1
    assert payload["segments"][0]["continuation_mode"] == "INITIAL"

    cross_project = client.get(f"/api/v1/projects/{uuid4()}/runs/{created['run_id']}/detail")
    assert cross_project.status_code == 404


def test_respond_to_interaction_creates_next_segment(client: TestClient) -> None:
    """認証済み回答が version と idempotency を固定して次 Segment を返す。"""

    fake = FakeRunService()
    client.app.state.run_service = fake
    project_id = uuid4()
    created = _create_task_run(client, project_id).json()
    interaction_id = uuid4()

    response = client.post(
        (
            f"/api/v1/projects/{project_id}/runs/{created['run_id']}"
            f"/interactions/{interaction_id}/responses"
        ),
        headers={"Idempotency-Key": "interaction-response-001"},
        json={"interaction_version": 1, "response": {"text": "Proceed with the review."}},
    )

    assert response.status_code == 201
    assert response.headers["Idempotent-Replay"] == "false"
    assert response.json()["status"] == "QUEUED"
    assert response.json()["segment_no"] == 2
    assert response.json()["continuation_mode"] == "REPLACE"
    assert fake.received_interaction_response == {"text": "Proceed with the review."}


@pytest.mark.parametrize(
    ("change", "status"),
    [("none", "FROZEN"), ("legacy", "LEGACY_UNAVAILABLE"),
     ("checksum", "INVALID"), ("project", "INVALID"), ("slot", "INVALID")],
)
def test_document_detail_projects_original_snapshot_without_private_fields(
    client: TestClient, change: str, status: str
) -> None:
    """公開 detail/history は同じ許可リストを使い、歴史欠損や破損を結果の欠落にしない。"""

    fake = FakeRunService()
    client.app.state.run_service = fake
    project_id = uuid4()
    created = _create_task_run(client, project_id).json()
    frozen = document_snapshot(
        project_id, [document_content(name="frozen-only.md")], key="documents"
    ).to_json()
    source: dict[str, Any] = {
        "provider": "project-documents", "capability": "document.read/v1",
        "resource_kind": "document", "access": "read", "document_snapshot": frozen,
        "scope": {"unpublished": "fixture-private"}, "secret_locator": "fixture-private",
    }
    if change == "legacy":
        source.pop("document_snapshot")
    elif change == "checksum":
        frozen["checksum"] = "sha256:" + "0" * 64
    elif change == "project":
        frozen["project_id"] = str(uuid4())
    elif change == "slot":
        frozen["requirement_key"] = "another-slot"
    sources: dict[str, Any] = {"documents": source}
    original = deepcopy(sources)
    fake.received_sources = sources
    response = client.get(f"/api/v1/projects/{project_id}/runs/{created['run_id']}/detail")
    assert response.status_code == 200
    payload = response.json()
    entry = payload["document_snapshots"][0]
    assert entry["status"] == status
    assert entry["snapshot"] == (frozen if status == "FROZEN" else None)
    assert payload["result"]["summary"] == "completed"
    assert sources == original
    assert "fixture-private" not in response.text
    if status != "FROZEN":
        assert "frozen-only.md" not in response.text
    assert set(payload["selected_sources"]["documents"]) == {
        "provider", "capability", "resource_kind", "access"
    }

    history = client.get(f"/api/v1/projects/{project_id}/runs").json()
    assert history["items"][0]["selected_sources"] == payload["selected_sources"]
    assert "document_snapshots" not in history["items"][0]
    assert "fixture-private" not in json.dumps(history)
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    for name, value in (("detail", payload), ("history", history)):
        schema = json.loads((contracts / "runs" / name / "v1.schema.json").read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    assert client.get(
        f"/api/v1/projects/{uuid4()}/runs/{created['run_id']}/detail"
    ).status_code == 404


def test_list_run_history_returns_paginated_project_items(client: TestClient) -> None:
    """Project Run history が pagination metadata と再表示用 input を返す。"""

    fake = FakeRunService()
    client.app.state.run_service = fake
    project_id = uuid4()
    _create_task_run(client, project_id)

    response = client.get(f"/api/v1/projects/{project_id}/runs?limit=10&offset=0")

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 10
    assert payload["offset"] == 0
    assert payload["has_more"] is False
    assert payload["items"][0]["input"]["target"] == "main"


def test_run_history_filters_by_status_on_the_server(client: TestClient) -> None:
    """待機中だけを出す一覧が SQL 側の絞り込みを使うことを確認する。

    画面側で先頭 page を filter すると、回答待ちの Run が古い page にあるときに取りこぼす。
    「待你处理」が漏れなく出ることは、絞り込みが server 側で効いていることに依存する。
    """

    fake = FakeRunService()
    client.app.state.run_service = fake
    project_id = uuid4()

    response = client.get(
        f"/api/v1/projects/{project_id}/runs",
        params={"status": ["WAITING_FOR_INPUT", "WAITING_FOR_APPROVAL"]},
    )

    assert response.status_code == 200
    assert [item.value for item in fake.history_statuses] == [
        "WAITING_FOR_INPUT",
        "WAITING_FOR_APPROVAL",
    ]


def test_run_history_without_status_filter_passes_no_constraint(client: TestClient) -> None:
    """絞り込み未指定では全件経路のままであることを確認する。"""

    fake = FakeRunService()
    client.app.state.run_service = fake

    response = client.get(f"/api/v1/projects/{uuid4()}/runs")

    assert response.status_code == 200
    assert fake.history_statuses == ()


def test_run_history_rejects_an_unknown_status(client: TestClient) -> None:
    """契約外の状態名は 422 で弾く。未知値を黙って無視すると全件が返ってしまう。"""

    client.app.state.run_service = FakeRunService()

    response = client.get(
        f"/api/v1/projects/{uuid4()}/runs",
        params={"status": "NOT_A_STATUS"},
    )

    assert response.status_code == 422
