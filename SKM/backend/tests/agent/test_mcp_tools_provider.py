"""新 MCP capability の原 binding・撤権・proposal・配備境界を検証する。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from skillmind.agent.mcp_tools_provider import McpToolsProvider
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.release import ExecutionFeatures
from skillmind.integrations.domain import IntegrationValidationError, normalize_integration_command
from tests.agent.test_mcp_provider import (
    context as context,
)
from tests.agent.test_mcp_provider import (
    database_resource as database_resource,
)
from tests.agent.test_mcp_provider import (
    mcp_context as mcp_context,
)
from tests.agent.test_mcp_provider import (
    resource as resource,
)
from tests.agent.test_mcp_tools import TOOLS, catalog, config, encoded
from tests.integrations.test_readonly_resources import command


@pytest.fixture
def tools_resource(resource):
    """同じ既存 binding を全五 tool の凍結契約へ変更する。"""
    integration = replace(
        resource.integration,
        config=config(),
        capabilities=("mcp.tools/v1", "mcp.query/v1", "mcp.call/v1"),
        scope={"resource_uris": [], "tool_names": sorted(TOOLS)},
    )
    return replace(resource, integration=integration, scope=integration.scope)


@pytest.fixture
def provider(tools_resource):
    """権限と network の独立 port を差し替える。"""
    source, leases = AsyncMock(), AsyncMock()
    source.discover.return_value = catalog()
    source.call.return_value = encoded({"status": "READY"})
    obj = McpToolsProvider(
        Mock(), capability="mcp.tools/v1", source=source, secret_resolver=Mock(), leases=leases
    )
    obj._binding._bound = AsyncMock(return_value=(tools_resource, "fixture-token"))
    return obj, source, leases


async def test_catalog_is_observed_twice_authorized_and_evidenced(provider, mcp_context):
    """遠端清單を一回読み、原 checksum と evidence locator を保持する。"""
    obj, source, _ = provider
    result = await obj.execute(mcp_context, {})
    assert obj._binding._bound.await_count == 2
    source.discover.assert_awaited_once()
    source.call.assert_not_awaited()
    assert result.evidence[0].source_locator["catalog_hash"] == result.response["catalog_hash"]


async def test_query_cannot_call_action_but_can_get_environment(provider, mcp_context):
    """新しい読取工具を名前 whitelist なしで呼び、変更工具は読取門で拒否する。"""
    obj, source, leases = provider
    obj._capability = "mcp.query/v1"
    with pytest.raises(ToolProviderError):
        await obj.execute(
            mcp_context, {"name": "open_application", "arguments": {"appId": "sample"}}
        )
    source.call.assert_not_awaited()
    source.call.return_value = encoded({"environmentVersion": "test-01"})
    result = await obj.execute(mcp_context, {"name": "get_environment", "arguments": {}})
    assert result.response["result"]["environmentVersion"] == "test-01"
    leases.acquire.assert_not_awaited()


async def test_revoked_binding_result_is_not_published(provider, mcp_context, tools_resource):
    """I/O 後に凭据が更新されたら本文と Evidence を返さない。"""
    obj, _, _ = provider
    obj._binding._bound.side_effect = [
        (tools_resource, "fixture-token"),
        (tools_resource, "changed"),
    ]
    with pytest.raises(ToolProviderError) as error:
        await obj.execute(mcp_context, {})
    assert error.value.code == "scope_denied"


def test_profile_scope_requires_readback_and_only_registered_mcp_run_consent():
    """起動時の既存自動承認同意と MCP の新操作を混同しない。"""
    original = replace(
        command("mcp"),
        secret_reference_id=uuid4(),
        config=config(),
        capabilities=("mcp.tools/v1", "mcp.query/v1", "mcp.call/v1"),
        scope={"resource_uris": [], "tool_names": sorted(TOOLS)},
    )
    assert normalize_integration_command(original).scope["resource_uris"] == []
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(replace(original, secret_reference_id=None))
    saved = normalize_integration_command(
        replace(original, scope={"resource_uris": [], "tool_names": ["open_application"]})
    )
    assert saved.scope["tool_names"] == ["open_application"]
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(
            replace(original, capabilities=("mcp.tools/v1", "mcp.query/v1"))
        )
    assert resolve_effect_capability("mcp.call/v1").supports_run_approval("mcp")
    assert not resolve_effect_capability("mcp.call/v1").supports_run_approval("other")
    assert not resolve_effect_capability("mcp.call/v1").supports_run_approval(None)
    assert not ExecutionFeatures(deferred=True).capability_enabled("mcp.call/v1")
    assert ExecutionFeatures(mcp_tools=True).effect_enabled("mcp.call/v1", "call", provider="mcp")


@pytest.mark.parametrize("bad_binding", [False, True])
async def test_action_proposal_requires_original_catalog_and_window_evidence(bad_binding):
    """Agent の自己申告ではなく、同じ Run の成功した工具発見だけを採用する。"""
    from types import SimpleNamespace
    from uuid import uuid4

    from skillmind.effects.domain import ChangeProposalValidationError
    from skillmind.integrations.mcp_tools import digest
    from skillmind.runs.repository import RunRepository

    binding = SimpleNamespace(run_id=uuid4(), integration_id=uuid4(), checksum="original")
    observed = [
        SimpleNamespace(
            metadata_json={"binding_checksum": "changed" if bad_binding else "original"},
            source_locator={"catalog_hash": digest(catalog())},
        )
    ]
    window = [
        SimpleNamespace(
            metadata_json={"binding_checksum": "original"},
            source_locator={
                "tool_name": "inspect_window",
                "result_status": "READY",
                "window_title": "Main",
                "arguments": {"appId": "sample"},
            },
        )
    ]
    session = AsyncMock()
    session.scalars.side_effect = [
        Mock(all=Mock(return_value=observed)),
        Mock(all=Mock(return_value=window)),
    ]
    repo = RunRepository(session)
    args = (
        SimpleNamespace(
            capability_version="mcp.call/v1", evidence_refs=("ev_catalog", "ev_window")
        ),
    )
    kwargs = {
        "binding": binding,
        "payload": {
            "name": "execute_step",
            "catalog_hash": digest(catalog()),
            "arguments": {"appId": "sample", "windowTitle": "Main"},
        },
    }
    if bad_binding:
        with pytest.raises(ChangeProposalValidationError):
            await repo._validate_mcp_observation(*args, **kwargs)
    else:
        await repo._validate_mcp_observation(*args, **kwargs)


def test_mcp_wiring_has_read_tools_and_supervised_effect_without_direct_action():
    """新能力は本番 registry へ装配され、外部 action は Agent gateway に登録しない。"""
    from pathlib import Path
    from uuid import uuid4

    from skillmind.agent.context_builder import ContractStore, create_run_tool_registry
    from skillmind.effects.wiring import create_effect_provider_registry

    contracts = ContractStore(Path(__file__).resolve().parents[3] / "contracts")
    registry = create_run_tool_registry(
        contracts,
        document_source=Mock(),
        mcp_tools_enabled=True,
        mcp_tools_provider=AsyncMock(),
        mcp_query_provider=AsyncMock(),
    )
    for capability in ("mcp.tools/v1", "mcp.query/v1"):
        assert (
            registry.resolve(
                capability, provider="mcp", integration_id=uuid4(), binding_id=uuid4()
            ).capability
            == capability
        )
    with pytest.raises(LookupError):
        registry.resolve("mcp.call/v1", provider="mcp", integration_id=uuid4(), binding_id=uuid4())
    effects = create_effect_provider_registry(
        features=ExecutionFeatures(mcp_tools=True),
        effect_service=Mock(),
        secret_resolver=Mock(),
        git_client=Mock(),
        svn_client=Mock(),
        mcp_leases=AsyncMock(),
    )
    definition = effects.resolve(capability_version="mcp.call/v1", provider="mcp")
    assert definition.supervised and definition.requires_secret
