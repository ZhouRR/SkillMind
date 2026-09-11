"""Integration、SecretReference と ResourceBinding 公開 API contract を検証する。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from skillmind.integrations.domain import (
    CreateIntegrationCommand,
    StoredIntegration,
    normalize_integration_command,
)
from tests.api.fakes import FakeIntegrationService


@pytest.mark.parametrize("provider", ["postgres", "mcp"])
def test_readonly_resource_registration_validates_and_hides_connection_config(
    client: TestClient,
    provider: str,
) -> None:
    """実 domain 検証を route と共有し、公開応答には config key のみを残す。"""
    from tests.integrations.test_readonly_resources import command

    class ValidatingService(FakeIntegrationService):
        """DB を使わず、本番と同じ登録時の domain 境界を通す。"""

        async def create_integration(self, value: CreateIntegrationCommand) -> StoredIntegration:
            """公開 command を正規化して既存 read model fake に渡す。"""
            return await super().create_integration(normalize_integration_command(value))

    service = ValidatingService()
    client.app.state.integration_service = service
    value = command(provider)
    body = {
        "name": value.name,
        "kind": value.kind,
        "provider": provider,
        "capabilities": list(value.capabilities),
        "scope": value.scope,
        "config": value.config,
        "secret_reference_id": str(value.secret_reference_id)
        if value.secret_reference_id
        else None,
    }
    response = client.post(f"/api/v1/projects/{value.project_id}/integrations", json=body)
    assert response.status_code == 201
    assert response.json()["provider"] == provider
    assert response.json()["scope"] == value.scope
    assert response.json()["config_keys"] == sorted(value.config)
    assert "config" not in response.json()
    refused = client.post(
        f"/api/v1/projects/{value.project_id}/integrations",
        json={**body, "config": {**value.config, "password": "fixture"}},
    )
    assert refused.status_code == 422


def test_admin_manages_secret_integration_and_exact_binding(client: TestClient) -> None:
    """Credential/connection 本文を response に出さず exact binding を保存する。"""

    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project_id = uuid4()
    secret = client.post(
        f"/api/v1/projects/{project_id}/secret-references",
        json={
            "name": "Redmine API token",
            "provider": "redmine",
            "resolver": "ENVIRONMENT",
            "locator": "REDMINE_API_KEY",
            "key_version": "v1",
        },
    )

    assert secret.status_code == 201
    assert service.received_locator == "REDMINE_API_KEY"
    assert "locator" not in secret.json()
    secret_id = secret.json()["secret_reference_id"]

    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        json={
            "name": "Audited Redmine",
            "kind": "issue",
            "provider": "redmine",
            "capabilities": ["issue.read/v1", "issue.update/v1"],
            "scope": {"issue_ids": ["1001"], "field_keys": ["status_id"]},
            "config": {"base_url": "https://redmine.example.invalid"},
            "secret_reference_id": secret_id,
        },
    )

    assert integration.status_code == 201
    assert service.received_config == {"base_url": "https://redmine.example.invalid"}
    assert "config" not in integration.json()
    assert integration.json()["config_keys"] == ["base_url"]
    integration_id = integration.json()["integration_id"]

    binding = client.put(
        f"/api/v1/projects/{project_id}/resource-bindings",
        json={
            "scope_level": "PROJECT_DEFAULT",
            "scope_key": "project",
            "requirement_key": "issue",
            "resource_kind": "issue",
            "integration_id": integration_id,
            "capability_version": "issue.update/v1",
            "requested_scope": {
                "issue_ids": ["1001"],
                "field_keys": ["status_id"],
            },
        },
    )

    assert binding.status_code == 200
    assert binding.json()["checksum"].startswith("sha256:")
    listed = client.get(f"/api/v1/projects/{project_id}/resource-bindings")
    assert listed.status_code == 200
    assert listed.json()["items"] == [binding.json()]


def test_admin_creates_managed_secret_without_exposing_plaintext(client: TestClient) -> None:
    """MANAGED は明文を渡して作成でき、response に locator も明文も出さない。"""

    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project_id = uuid4()

    created = client.post(
        f"/api/v1/projects/{project_id}/secret-references",
        json={
            "name": "Managed Redmine token",
            "provider": "redmine",
            "resolver": "MANAGED",
            "key_version": "v1",
            "secret_value": "super-secret-token",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["resolver"] == "MANAGED"
    assert "locator" not in body
    assert "secret_value" not in body
    # 明文は command 経由でだけ渡り、locator は sentinel に固定される。
    assert service.received_secret_value == "super-secret-token"
    assert service.received_locator == "managed"


def test_managed_secret_rejects_external_locator(client: TestClient) -> None:
    """MANAGED に locator を渡すと 422 で拒否する。"""

    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project_id = uuid4()

    rejected = client.post(
        f"/api/v1/projects/{project_id}/secret-references",
        json={
            "name": "Managed token",
            "provider": "redmine",
            "resolver": "MANAGED",
            "locator": "REDMINE_API_KEY",
            "key_version": "v1",
            "secret_value": "token",
        },
    )

    assert rejected.status_code == 422


def test_deployment_secret_requires_locator(client: TestClient) -> None:
    """ENVIRONMENT/FILE は locator 無しだと 422 で拒否する。"""

    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project_id = uuid4()

    rejected = client.post(
        f"/api/v1/projects/{project_id}/secret-references",
        json={
            "name": "Env token",
            "provider": "redmine",
            "resolver": "ENVIRONMENT",
            "key_version": "v1",
        },
    )

    assert rejected.status_code == 422


def test_integration_disable_requires_expected_revision(client: TestClient) -> None:
    """Disable request が表示中の optimistic revision を service へ渡す。"""

    service = FakeIntegrationService()
    client.app.state.integration_service = service
    project_id = uuid4()
    created = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        json={
            "name": "Read-only Git",
            "kind": "repository",
            "provider": "git",
            "capabilities": ["repository.read/v1"],
            "scope": {"paths": ["src"], "revisions": ["HEAD"]},
            "config": {
                "repository_uri": "https://git.example.invalid/project",
                "default_revision": "HEAD",
            },
            "secret_reference_id": None,
        },
    ).json()

    disabled = client.post(
        (
            f"/api/v1/projects/{project_id}/integrations/"
            f"{created['integration_id']}/disable"
        ),
        json={"expected_revision": 1},
    )

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    assert disabled.json()["revision"] == 2


def test_managed_secret_without_kek_is_service_unavailable(client: TestClient) -> None:
    """KEK 未配線の配備では 500 ではなく、原因の判る 503 Problem を返す。"""

    service = FakeIntegrationService()
    service.managed_secret_unavailable = True
    client.app.state.integration_service = service

    response = client.post(
        f"/api/v1/projects/{uuid4()}/secret-references",
        json={
            "name": "Managed Redmine token",
            "provider": "redmine",
            "resolver": "MANAGED",
            "key_version": "v1",
            "secret_value": "super-secret-token",
        },
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "managed_secret_unavailable"
    # 明文は Problem のどこにも反射しない。
    assert "super-secret-token" not in response.text
