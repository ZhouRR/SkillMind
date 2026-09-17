"""MCP 発見 endpoint の ADMIN/CSRF、revision、失権と固定接続を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock

from skillmind.auth.sessions import UnauthorizedSessionError
from tests.agent.test_mcp_tools import catalog
from tests.api.test_resource_lifecycle_api import create_resource


def test_discovery_reads_saved_connection_without_mutation(client):
    """URL/秘密の送信を受け付けず、指定 revision の read-only service のみを呼ぶ。"""
    service, path, _ = create_resource(client)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"mcp_tools_enabled": True}
    )
    service.discover_mcp_tools = AsyncMock(return_value=catalog())
    response = client.post(path + "/mcp-tools/discover", json={"expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["catalog"] == catalog()
    assert len(service.integrations) == 1 and service.integrations[0].revision == 1
    assert (
        client.post(
            path + "/mcp-tools/discover",
            json={"expected_revision": 1, "server_url": "https://other.example.test"},
        ).status_code
        == 422
    )
    assert service.discover_mcp_tools.await_count == 1


def test_discovery_flag_and_csrf_gate_before_remote(client):
    """全後置機能が on でも MCP の独立 switch と CSRF を迂回しない。"""
    service, path, _ = create_resource(client)
    service.discover_mcp_tools = AsyncMock(return_value=catalog())
    assert (
        client.post(path + "/mcp-tools/discover", json={"expected_revision": 1}).status_code == 409
    )
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"mcp_tools_enabled": True}
    )
    client.headers.pop("X-CSRF-Token")
    assert (
        client.post(path + "/mcp-tools/discover", json={"expected_revision": 1}).status_code == 422
    )
    service.discover_mcp_tools.assert_not_awaited()


def test_discovery_reauthenticates_after_io(client):
    """発見中に session を失効させても清單を返さない。"""
    service, path, _ = create_resource(client)
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"mcp_tools_enabled": True}
    )

    async def revoke(**kwargs):
        """外部応答到着時点の撤権を模擬する。"""
        client.app.state.auth_service.authenticate_unsafe_session = AsyncMock(
            side_effect=UnauthorizedSessionError("revoked")
        )
        return catalog()

    service.discover_mcp_tools = AsyncMock(side_effect=revoke)
    response = client.post(path + "/mcp-tools/discover", json={"expected_revision": 1})
    assert response.status_code == 401
    assert "FlaUiMcp" not in response.text
