"""共有 HTTP policy を保持し、観測・提案と凍結 binding の境界を検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest

from skillmind.agent.context_builder import ProductionRunContextBuilder
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import create_run_tool_registry
from skillmind.agent.workspace import WorkspaceManager
from skillmind.effects.release import ExecutionFeatures
from skillmind.integrations.domain import ResourceBindingLevel, binding_checksum
from tests.agent.test_runtime_context import CONTRACTS, _generic_claimed, _generic_manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("valid_binding", [False, True])
async def test_http_context_keeps_policy_and_requires_frozen_binding(
    tmp_path: Path, enabled: bool, valid_binding: bool,
) -> None:
    """HTTP 単独許可でも直接 write Tool や不正 binding を実行へ昇格させない。"""

    capabilities = ("http.read/v1", "http.write/v1", "change.propose/v1")
    manifest = _generic_manifest()
    manifest["tools"] = [{"capability": cap, "required": True} for cap in capabilities]
    blueprint = manifest["capability_blueprint"]
    blueprint["resource_requirements"] = [{
        "key": "api", "kind": "other", "required": True, "access": "write",
        "capabilities": list(capabilities[:2]), "accepted_providers": ["http"],
    }]
    blueprint["effect_intents"] = [{
        "key": "update", "resource_key": "api", "mode": "apply",
        "operation": "PATCH", "risk": "medium", "approval_mode": "ask",
    }]
    claimed = _generic_claimed(
        selected_sources={"api": {"capability": "http.read/v1", "provider": "http"}},
        allowed=capabilities, manifest=manifest,
    )
    source = claimed.selected_sources_json["api"]
    source.update(binding_capability="http.write/v1", access="write")
    source["binding_checksum"] = binding_checksum(
        project_id=claimed.project_id, scope_level=ResourceBindingLevel.RUN,
        scope_key=str(claimed.run_id), requirement_key="api", resource_kind="other",
        integration_id=UUID(source["integration_id"]), provider="http",
        capability_version="http.write/v1", revision="1", scope={},
    )
    if not valid_binding:
        source.pop("integration_id")
    provider = Mock()
    workspace = WorkspaceManager((tmp_path / "runs").resolve())
    features = ExecutionFeatures(http_writes=enabled)
    builder = ProductionRunContextBuilder(
        workspace_manager=workspace,
        tool_registry=create_run_tool_registry(
            ContractStore(CONTRACTS), document_source=Mock(),
            http_provider=provider, deferred_features_enabled=False,
        ),
        model="test-model", execution_features=features,
    )
    assert builder._execution_features is features
    if not enabled or not valid_binding:
        message = "disabled execution features" if not enabled else "frozen Integration binding"
        with pytest.raises(ValueError, match=message):
            await builder.build(claimed, sequence_start=1)
        assert not (tmp_path / "runs").exists()
    else:
        context = await builder.build(claimed, sequence_start=1)
        assert {tool.capability for tool in context.tools} == {"http.read/v1", "change.propose/v1"}
        assert features.write_capabilities == frozenset({"http.write/v1"})
    provider.execute.assert_not_called()
