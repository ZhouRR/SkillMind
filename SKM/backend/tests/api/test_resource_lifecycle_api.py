"""資源編集・削除 API の認可、原 version と公開 projection を検証する。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from skillmind.integrations.domain import IntegrationConflictError, IntegrationNotFoundError
from tests.api.fakes import FakeIntegrationService


def create_resource(client: TestClient) -> tuple[FakeIntegrationService, str, dict]:
    """実 route を通して合成接続の metadata を作成する。"""
    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project = str(uuid4())
    body = {
        "name": "Review database",
        "kind": "other",
        "provider": "mcp",
        "capabilities": ["mcp.read/v1"],
        "scope": {"resource_uris": ["resource://reports/current"]},
        "config": {"server_url": "https://mcp.example.test/mcp", "transport": "streamable_http"},
        "secret_reference_id": None,
    }
    result = client.post(f"/api/v1/projects/{project}/integrations", json=body)
    assert result.status_code == 201
    return (
        service,
        f"/api/v1/projects/{project}/integrations/{result.json()['integration_id']}",
        body,
    )


def test_admin_edits_revision_and_reads_only_non_secret_config(client: TestClient) -> None:
    """詳細と一覧を分け、更新の原 revision と非機密設定を service へ渡す。"""
    service, path, body = create_resource(client)
    service.get_integration_details = AsyncMock(
        return_value=(service.integrations[0], body["config"])
    )
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["config"] == body["config"]
    assert "config" not in response.json()["integration"]
    service.update_integration = AsyncMock(
        return_value=replace(service.integrations[0], revision=2)
    )
    response = client.put(path, json={**body, "expected_revision": 1})
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert "config" not in response.json()
    args = service.update_integration.call_args
    assert args.kwargs["expected_revision"] == 1
    assert args.args[0].config == body["config"]


@pytest.mark.parametrize(
    "failure,status",
    [
        (IntegrationConflictError("Integration is referenced"), 409),
        (IntegrationNotFoundError("Integration not found in project"), 404),
    ],
)
def test_delete_maps_reference_and_ownership_failures(
    client: TestClient,
    failure: Exception,
    status: int,
) -> None:
    """service の参照保護と所有検査を安定した公開 Problem に写す。"""
    service, path, _ = create_resource(client)
    service.delete_integration = AsyncMock(side_effect=failure)
    assert client.delete(path, params={"expected_revision": 1}).status_code == status


def test_delete_requires_original_revision_and_returns_empty_success(client: TestClient) -> None:
    """原 revision 不明の削除を拒否し、成功は 204 のみ返す。"""
    service, path, _ = create_resource(client)
    service.delete_integration = AsyncMock()
    assert client.delete(path).status_code == 422
    service.delete_integration.assert_not_awaited()
    result = client.delete(path, params={"expected_revision": 1})
    assert result.status_code == 204 and result.content == b""
    assert service.delete_integration.call_args.kwargs["expected_revision"] == 1


def test_secret_edit_omits_original_value_and_delete_requires_timestamp(client: TestClient) -> None:
    """本文未指定を保持指定として送り、返却値や削除 query へ本文を混ぜない。"""
    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project = str(uuid4())
    created = client.post(
        f"/api/v1/projects/{project}/secret-references",
        json={
            "name": "Review password",
            "provider": "postgres",
            "resolver": "MANAGED",
            "key_version": "v1",
            "secret_value": "synthetic-only",
        },
    ).json()
    path = f"/api/v1/projects/{project}/secret-references/{created['secret_reference_id']}"
    service.update_secret_reference = AsyncMock(return_value=service.secrets[0])
    result = client.patch(
        path,
        json={"name": "Renamed", "key_version": "v1", "expected_updated_at": created["updated_at"]},
    )
    assert result.status_code == 200
    assert service.update_secret_reference.call_args.args[0].secret_value is None
    assert "synthetic-only" not in result.text and "locator" not in result.json()
    service.delete_secret_reference = AsyncMock()
    assert client.delete(path).status_code == 422
    assert (
        client.delete(path, params={"expected_updated_at": created["updated_at"]}).status_code
        == 204
    )


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_resource_management_rejects_non_admin(client: TestClient, method: str) -> None:
    """詳細・編集・削除が共通 ADMIN 境界を迂回しない。"""
    _, path, body = create_resource(client)
    auth = client.app.state.auth_service
    auth.actor = replace(auth.actor, system_role="MEMBER")
    response = client.request(
        method, path, json={**body, "expected_revision": 1}, params={"expected_revision": 1}
    )
    assert response.status_code == 403


def test_edit_and_delete_reject_missing_csrf(client: TestClient) -> None:
    """新しい mutation も共通 Origin/CSRF 検査を通す。"""
    _, path, body = create_resource(client)
    del client.headers["X-CSRF-Token"]
    assert client.put(path, json={**body, "expected_revision": 1}).status_code == 422
    assert client.delete(path, params={"expected_revision": 1}).status_code == 422
