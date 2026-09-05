"""Run 作成・取消・interaction・detail・history API の契約を検証する。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fakes import FakeAuthService, FakeRunService, FakeSkillService
from fastapi.testclient import TestClient

from projectmind.auth.service import AuthenticatedActor


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

    response = client.get(
        f"/api/v1/projects/{project_id}/runs/{created['run_id']}/detail"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["summary"] == "completed"
    assert payload["tool_calls"][0]["capability"] == "repository.read/v1"
    assert "result_json" not in payload["tool_calls"][0]
    assert payload["evidence"][0]["evidence_ref"] == "ev_repo_001"
    assert payload["segments"][0]["segment_no"] == 1
    assert payload["segments"][0]["continuation_mode"] == "INITIAL"

    cross_project = client.get(
        f"/api/v1/projects/{uuid4()}/runs/{created['run_id']}/detail"
    )
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
