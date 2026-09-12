"""認証済みの後置操作を拒否し、首版の読み取りと停止操作は維持する。"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.api.fakes import FakeEffectService, FakeScheduleService


@pytest.mark.parametrize(
    "operation", ["schedule-create", "schedule-edit", "schedule-enable", "policy"]
)
def test_readonly_api_rejects_deferred_mutations_before_service(client, operation):
    """同じ Project の ADMIN と有効 CSRF でも、配備上限を越えた mutation は実行しない。"""
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False}
    )
    service = AsyncMock()
    client.app.state.schedule_service = service
    client.app.state.effect_service = service
    client.app.state.run_service = service
    project, record = uuid4(), uuid4()
    method = "POST"
    body = {
        "name": "Example",
        "definition": {"kind": "ONCE", "timezone": "UTC", "run_at": "2099-01-01T00:00:00Z"},
        "skill_version_id": str(uuid4()),
        "task_key": "review",
    }
    path = f"/api/v1/projects/{project}/schedules"
    if operation == "schedule-edit":
        method, path = "PUT", path + f"/{record}"
        del body["skill_version_id"], body["task_key"]
        body["expected_row_version"] = 1
    elif operation == "schedule-enable":
        path += f"/{record}/status"
        body = {"status": "ACTIVE", "expected_row_version": 1}
    elif operation == "policy":
        path = f"/api/v1/projects/{project}/effect-preauthorizations"
        body = {
            "integration_id": str(record),
            "capability_version": "issue.update/v1",
            "operation": "update_fields",
            "risk_level": "LOW",
            "scope": {"issue_ids": ["1"], "field_keys": ["status_id"]},
            "expires_at": "2099-01-01T00:00:00Z",
        }
    response = client.request(
        method, path, json=body, headers={"Idempotency-Key": "fixture-release"}
    )
    assert response.status_code == 409 and response.json()["code"] == "feature_not_enabled"
    assert response.headers["cache-control"] == "no-store"
    assert service.mock_calls == []


def test_readonly_resource_and_schedule_lists_remain_readable(client):
    """後置 mutation の停止で、核心ページが依存する既存 GET を一緒に閉じない。"""
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False}
    )
    client.app.state.schedule_service = FakeScheduleService()
    client.app.state.effect_service = FakeEffectService()
    project = uuid4()
    for path in ("schedules", "effect-preauthorizations"):
        assert client.get(f"/api/v1/projects/{project}/{path}").status_code == 200


@pytest.mark.parametrize("status", ["PAUSED", "ARCHIVED"])
def test_readonly_schedule_can_still_be_stopped(client, status):
    """後置機能を無効化しても、現在の資格と原版で既存調度を停止できる。"""
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False}
    )
    client.app.state.schedule_service = FakeScheduleService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/schedules/{uuid4()}/status",
        json={"status": status, "expected_row_version": 1},
    )
    assert response.status_code == 200 and response.json()["status"] == status
