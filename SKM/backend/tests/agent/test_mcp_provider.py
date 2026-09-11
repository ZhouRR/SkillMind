"""MCP の原 binding、撤権、Evidence と実装装配を外部接続なしで検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.context_builder import ContractStore, _read_tool_definitions
from skillmind.agent.mcp_provider import McpReadProvider
from skillmind.agent.mcp_source import McpReadError
from skillmind.agent.run_binding import RunBindingError
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.domain import INSTALLED_PROVIDER_CAPABILITIES
from skillmind.skills.interpreter import load_capability_catalog
from skillmind.skills.manifest_gate import ManifestValidator
from skillmind.skills.resource_binding import (
    ProjectResourceCandidate,
    TaskReadinessLevel,
    evaluate_blueprint_readiness,
)
from tests.agent import test_database_provider

URI = "resource://reports/current"
ROOT = Path(__file__).resolve().parents[3]
context = test_database_provider.context
database_resource = test_database_provider.resource


@pytest.fixture
def resource(database_resource):
    """原 binding の共通 fixture を optional credential の MCP 構成へ変換する。"""
    integration = replace(
        database_resource.integration,
        provider="mcp",
        capabilities=("mcp.read/v1",),
        scope={"resource_uris": [URI]},
        secret_reference_id=None,
        config={"server_url": "https://mcp.example.test/mcp", "transport": "streamable_http"},
    )
    return replace(database_resource, integration=integration, scope=integration.scope)


@pytest.fixture
def mcp_context(context):
    """Gateway が固定する Tool identity だけを MCP の能力へ変更する。"""
    return replace(context, tool=replace(context.tool, capability="mcp.read/v1", provider="mcp"))


@pytest.fixture
def provider(monkeypatch, resource):
    """認可 DB と source を mock にし、Provider の検証/応答本体を動かす。"""
    bound = AsyncMock(return_value=resource)
    secret = AsyncMock(return_value=None)
    monkeypatch.setattr("skillmind.agent.mcp_provider.load_bound_run_resource", bound)
    monkeypatch.setattr("skillmind.agent.mcp_provider.resolve_binding_secret", secret)
    source = AsyncMock()
    source.read.return_value = ({"uri": URI, "text": "Example"},)
    return (
        McpReadProvider(Mock(return_value=AsyncMock()), source=source, secret_resolver=Mock()),
        source,
        bound,
        secret,
    )


@pytest.mark.asyncio
async def test_original_binding_is_rechecked_and_content_hash_matches_evidence(
    provider, mcp_context
):
    """MCP 本文と読み取り時点を同じ hash で証拠化し、接続設定を公開しない。"""
    implementation, source, bound, secret = provider
    result = await implementation.execute(mcp_context, {"uri": URI, "purpose": "Review"})
    assert bound.await_count == secret.await_count == 2
    assert source.read.await_count == 1
    for call in bound.await_args_list:
        assert call.kwargs["binding_id"] == mcp_context.tool.binding_id
        assert call.kwargs["run_id"] == mcp_context.run_id
        assert call.kwargs["provider"] == "mcp" and call.kwargs["capability"] == "mcp.read/v1"
    schema = ContractStore(ROOT / "contracts").load("tools/mcp.read/v1/response.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        {**result.response, "evidence_refs": ["ev_example"]}
    )
    content = {key: result.response[key] for key in ("uri", "contents", "read_at")}
    assert result.evidence[0].source_uri == URI
    assert result.response["content_hash"] == result.evidence[0].content_hash
    assert result.response["content_hash"] == "sha256:" + sha256_hex(canonical_json(content))
    assert "mcp.example.test" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments", [{"uri": "resource://other"}, {"uri": URI, "method": "tools/call"}, {"uri": 1}]
)
async def test_outside_scope_or_protocol_override_never_contacts_server(
    provider, mcp_context, arguments
):
    """入力は URI 選択以外の network 能力を増やせない。"""
    implementation, source, _, _ = provider
    with pytest.raises(ToolProviderError):
        await implementation.execute(mcp_context, arguments)
    source.read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["revoked", "rotated", "revision"])
async def test_changes_during_read_discard_contents(provider, mcp_context, resource, changed):
    """読取後の撤権・資格情報輪換・接続版変更では Evidence を発行しない。"""
    implementation, _, bound, secret = provider
    if changed == "revoked":
        bound.side_effect = [
            resource,
            RunBindingError("unavailable", "Resource revoked", retryable=False),
        ]
    elif changed == "rotated":
        secret.side_effect = [None, "fixture-token"]
    else:
        bound.side_effect = [
            resource,
            replace(resource, integration=replace(resource.integration, revision=2)),
        ]
    with pytest.raises(ToolProviderError):
        await implementation.execute(mcp_context, {"uri": URI})


@pytest.mark.asyncio
async def test_transport_detail_is_not_published(provider, mcp_context):
    """Provider は transport 例外の本文や credential を Tool error に含めない。"""
    implementation, source, _, _ = provider
    source.read.side_effect = McpReadError("fixture-private-detail")
    with pytest.raises(ToolProviderError) as failure:
        await implementation.execute(mcp_context, {"uri": URI})
    assert failure.value.code == "unavailable" and "fixture-private-detail" not in str(
        failure.value
    )


def test_registry_requires_explicit_mcp_source(provider):
    """resource Tool だけを装配し、遠端 tools は registry に直接追加しない。"""
    contracts = ContractStore(ROOT / "contracts")
    assert _read_tool_definitions(contracts) == ()
    definitions = _read_tool_definitions(contracts, mcp_provider=provider[0])
    assert len(definitions) == 1 and definitions[0].capability == "mcp.read/v1"
    assert set(definitions[0].providers) == {"mcp"}


def test_mcp_catalog_and_resource_readiness_reach_runnable(resource):
    """説明用の登録だけで止まらず、実装済み資源と能力が Task へ解決される。"""
    catalog = load_capability_catalog(ROOT / "contracts/examples/skill-capability-catalog.v1.json")
    entry = next(item for item in catalog.capabilities if item.capability == "mcp.read/v1")
    assert entry.providers == ("mcp",)
    readiness = evaluate_blueprint_readiness(
        {
            "resource_requirements": [
                {
                    "key": "reports",
                    "kind": "other",
                    "required": True,
                    "access": "read",
                    "capabilities": [entry.capability],
                    "accepted_providers": ["mcp"],
                }
            ]
        },
        candidates=[
            ProjectResourceCandidate(
                key="reports",
                kind="other",
                provider="mcp",
                label="Reports",
                capabilities=(entry.capability,),
                integration_id=resource.integration.integration_id,
                scope=resource.scope,
            )
        ],
        registered_capabilities=ManifestValidator(ROOT / "contracts").registered_capabilities,
        installed_provider_capabilities=INSTALLED_PROVIDER_CAPABILITIES,
    )
    assert readiness.level is TaskReadinessLevel.RUNNABLE
