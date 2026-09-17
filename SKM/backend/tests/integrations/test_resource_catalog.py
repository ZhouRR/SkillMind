"""空の MCP 範囲を実行可能候補として公開しないことを確認する。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from skillmind.integrations.resource_catalog import IntegrationResourceCatalog
from tests.integrations.test_resource_lifecycle import integration_fixture


@pytest.mark.asyncio
async def test_empty_mcp_connection_remains_stored_but_is_not_a_task_candidate(monkeypatch):
    """資格情報保存用の接続と、読取可能な URI を持つ接続を区別する。"""
    repo, _, row = integration_fixture()
    from tests.integrations.test_readonly_resources import command

    stored = await repo.update_integration(
        replace(command("mcp"), project_id=row.project_id, scope={"resource_uris": []}),
        integration_id=row.id,
        expected_revision=2,
    )
    listing = AsyncMock(return_value=(stored,))
    monkeypatch.setattr(
        "skillmind.integrations.resource_catalog.IntegrationRepository",
        lambda session: Mock(list_integrations=listing),
    )
    catalog = IntegrationResourceCatalog(Mock(return_value=AsyncMock()))
    assert await catalog.candidates(project_id=row.project_id) == ()
    listing.assert_awaited_once_with(project_id=row.project_id, active_only=True)
    listing.return_value = (replace(stored, scope={"resource_uris": ["resource://reports/current"]}),)
    candidates = await catalog.candidates(project_id=row.project_id)
    assert len(candidates) == 1
    assert candidates[0].integration_id == row.id
    assert candidates[0].capabilities == ("mcp.read/v1",)

    listing.return_value = (replace(stored,
        scope={"resource_uris": [], "tool_names": ["inspect_window", "get_step_status"]},
        capabilities=("mcp.read/v1", "mcp.tools/v1", "mcp.query/v1")),)
    candidates = await catalog.candidates(project_id=row.project_id)
    assert len(candidates) == 1
    assert candidates[0].capabilities == ("mcp.tools/v1", "mcp.query/v1")
