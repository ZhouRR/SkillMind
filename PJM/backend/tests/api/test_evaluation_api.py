"""追加式 Evaluation API の契約を検証する。"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

from fakes import FakeAuthService, FakeEvaluationService
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_create_and_list_evaluation_preserves_original_and_suggestion(
    client: TestClient,
) -> None:
    """Evaluation API が AI 原値と人工提案値を分離して追加・再取得する。"""

    service = FakeEvaluationService()
    app = cast(FastAPI, client.app)
    app.state.evaluation_service = service
    project_id = uuid4()
    run_id = uuid4()
    response = client.post(
        f"/api/v1/projects/{project_id}/runs/{run_id}/evaluations",
        json={
            "rating": 4,
            "verdict": "partially_accurate",
            "comment": "需要修正字段",
            "revisions": [
                {
                    "pointer": "/fields/0/value",
                    "suggested_value": "after",
                    "reason": "Evidence confirmed",
                }
            ],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["revisions"][0]["original_value"] == "before"
    assert payload["revisions"][0]["suggested_value"] == "after"
    assert service.received is not None
    auth = app.state.auth_service
    assert isinstance(auth, FakeAuthService)
    assert service.received.user_id == auth.actor.user_id
    assert service.accesses[0].actor == auth.actor
    assert service.accesses[0].csrf_token == auth.csrf_token

    history = client.get(f"/api/v1/projects/{project_id}/runs/{run_id}/evaluations")
    assert history.status_code == 200
    assert history.json()["items"] == [payload]
    assert len(service.accesses) == 2
    assert history.headers["cache-control"] == "no-store"


def test_create_evaluation_rejects_invalid_pointer(client: TestClient) -> None:
    """存在しない Result pointer が安定した 400 Problem response になる。"""

    app = cast(FastAPI, client.app)
    app.state.evaluation_service = FakeEvaluationService(invalid_revision=True)
    response = client.post(
        f"/api/v1/projects/{uuid4()}/runs/{uuid4()}/evaluations",
        json={
            "rating": 2,
            "verdict": "inaccurate",
            "comment": "invalid",
            "revisions": [
                {"pointer": "/missing", "suggested_value": 1, "reason": "Missing field"}
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_evaluation_revision"
