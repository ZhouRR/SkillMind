"""MCP の複数読取能力を同じ凍結接続へ渡し、実 ContextBuilder の準備を検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
from skillmind.agent.context_builder import (
    ProductionRunContextBuilder,
)
from skillmind.agent.contract_store import (
    ContractStore,
)
from skillmind.agent.tool_catalog import (
    create_run_tool_registry,
)
from skillmind.agent.workspace import WorkspaceManager
from skillmind.integrations.domain import ResourceBindingLevel, binding_checksum
from tests.agent.test_runtime_context import (
    CONTRACTS,
    _generic_claimed,
    _generic_manifest,
    _NoopDocumentSource,
)

READS = ('mcp.tools/v1', 'mcp.query/v1')


def mcp_claim(*, primary='mcp.query/v1', writable=True, required=True, declarations=READS,
              extra_resource=False):
    """実障害と同じ、複数 capability に対する一つの選択済み source を作る。"""
    manifest = _generic_manifest()
    capabilities = (*declarations, 'mcp.call/v1') if writable else declarations
    manifest['tools'] = [
        {'capability': cap, 'required': required} for cap in READS
    ]
    blueprint = manifest['capability_blueprint']
    resource = {
        'key': 'runner', 'kind': 'other', 'required': True,
        'access': 'write' if writable else 'read', 'capabilities': list(capabilities),
        'accepted_providers': ['mcp'],
    }
    blueprint['resource_requirements'] = [resource]
    if writable:
        blueprint['effect_intents'] = [{
            'key': 'step', 'resource_key': 'runner', 'mode': 'apply',
            'operation': 'call', 'risk': 'low',
        }]
    selected = {'runner': {'capability': primary, 'provider': 'mcp'}}
    if extra_resource:
        blueprint['resource_requirements'].append({**deepcopy(resource), 'key': 'other-runner'})
        selected['other-runner'] = {'capability': READS[0], 'provider': 'mcp'}
    allowed = (*READS, 'change.propose/v1') if writable else READS
    claimed = _generic_claimed(manifest=manifest, allowed=allowed, selected_sources=selected)
    for key, source in claimed.selected_sources_json.items():
        source.update(
            binding_capability='mcp.call/v1' if writable else primary,
            access='write' if writable else 'read',
            scope={'resource_uris': [], 'tool_names': ['inspect_window', 'get_step_status']},
        )
        source['binding_checksum'] = binding_checksum(
            project_id=claimed.project_id, scope_level=ResourceBindingLevel.RUN,
            scope_key=str(claimed.run_id), requirement_key=key, resource_kind='other',
            integration_id=UUID(source['integration_id']), provider='mcp',
            capability_version=source['binding_capability'], revision='1', scope=source['scope'],
        )
    return claimed


def context_builder(tmp_path: Path, *, enabled=True, install_companion=True):
    """本番 registry を使うが、Provider は準備中の I/O を検出する double とする。"""
    catalog, query = Mock(), Mock()
    registry = create_run_tool_registry(
        ContractStore(CONTRACTS), document_source=_NoopDocumentSource(),
        mcp_tools_provider=catalog if install_companion else None, mcp_query_provider=query,
        deferred_features_enabled=False, mcp_tools_enabled=enabled,
    )
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(tmp_path / 'runs'), tool_registry=registry,
        model='test-model', deferred_features_enabled=False, mcp_tools_enabled=enabled,
    )
    return builder, registry, catalog, query


@pytest.mark.parametrize('primary', READS)
@pytest.mark.parametrize('writable', [False, True])
@pytest.mark.parametrize('policy', [None, 'skillmind.runtime/v3'])
async def test_mcp_context_binds_both_reads_without_exposing_effect(
    tmp_path, primary, writable, policy,
):
    """選択の代表能力・読書権に依らず、宣言済み二能力を原 binding へ渡す。"""
    claimed = mcp_claim(primary=primary, writable=writable)
    if policy is not None:
        claimed = replace(
            claimed, task_snapshot_json={**claimed.task_snapshot_json, 'runtime_policy': policy},
        )
    builder, registry, catalog, query = context_builder(tmp_path)
    frozen = deepcopy((claimed.selected_sources_json, claimed.permission_snapshot_json,
                       claimed.skill_snapshots_json))
    context = await builder.build(claimed, sequence_start=1)
    bound = [tool for tool in context.tools if tool.capability in READS]
    assert {tool.capability for tool in bound} == set(READS)
    source = claimed.selected_sources_json['runner']
    assert {str(tool.binding_id) for tool in bound} == {source['binding_id']}
    assert {str(tool.integration_id) for tool in bound} == {source['integration_id']}
    assert {tool.provider for tool in bound} == {'mcp'}
    assert 'mcp.call/v1' not in {tool.capability for tool in context.tools}
    registry.build_gateway_runtime(context, audit_writer=Mock())
    assert (claimed.selected_sources_json, claimed.permission_snapshot_json,
            claimed.skill_snapshots_json) == frozen
    assert not catalog.mock_calls and not query.mock_calls


@pytest.mark.parametrize('required', [False, True])
async def test_mcp_companion_requires_original_permission(tmp_path, required):
    """旧 Run の権限にない能力を同じ接続から勝手に追加しない。"""
    claimed = mcp_claim(required=required)
    permission = deepcopy(claimed.permission_snapshot_json)
    permission['allowed_capabilities'].remove('mcp.tools/v1')
    claimed = replace(claimed, permission_snapshot_json=permission)
    builder, _, _, _ = context_builder(tmp_path)
    if required:
        with pytest.raises(ValueError, match='not allowed'):
            await builder.build(claimed, sequence_start=1)
    else:
        context = await builder.build(claimed, sequence_start=1)
        assert 'mcp.tools/v1' not in {tool.capability for tool in context.tools}


async def test_mcp_companion_cannot_borrow_undeclared_resource(tmp_path):
    """同じ provider の接続が存在しても、資源宣言外の能力へ流用しない。"""
    builder, _, _, _ = context_builder(tmp_path)
    with pytest.raises(LookupError, match=r'requires a resource binding: mcp\.tools/v1'):
        await builder.build(mcp_claim(declarations=('mcp.query/v1',)), sequence_start=1)


@pytest.mark.parametrize('required', [False, True])
async def test_mcp_uninstalled_companion_keeps_required_semantics(tmp_path, required):
    """任意 Tool の欠落を許し、必須 Tool は未設定 Provider を合成せず拒否する。"""
    builder, _, _, _ = context_builder(tmp_path, install_companion=False)
    if required:
        with pytest.raises(LookupError, match=r'not installed: mcp\.tools/v1'):
            await builder.build(mcp_claim(required=required), sequence_start=1)
    else:
        context = await builder.build(mcp_claim(required=required), sequence_start=1)
        assert 'mcp.tools/v1' not in {tool.capability for tool in context.tools}


async def test_mcp_binding_checksum_is_checked_before_companion_resolution(tmp_path):
    """scope の改竄を伴う binding から追加能力を公開しない。"""
    claimed = mcp_claim()
    claimed.selected_sources_json['runner']['scope']['tool_names'].append('open_application')
    builder, _, _, _ = context_builder(tmp_path)
    with pytest.raises(ValueError, match='checksum'):
        await builder.build(claimed, sequence_start=1)


async def test_mcp_disabled_feature_still_rejects_context(tmp_path):
    """Provider が登録済みでも、配備上限を越えない。"""
    builder, _, _, _ = context_builder(tmp_path, enabled=False)
    with pytest.raises(ValueError, match='disabled execution features'):
        await builder.build(mcp_claim(), sequence_start=1)


async def test_mcp_multiple_resources_are_explicit_and_never_choose_first_binding(tmp_path):
    """複数接続の準備は許可し、呼出し時の曖昧さは未実行で拒否する。"""
    from skillmind.agent.tool_policy import (
        ToolExecutionPolicy,
        ToolPolicyViolation,
        capability_to_sdk_name,
    )

    builder, registry, _, _ = context_builder(tmp_path)
    claimed = mcp_claim(extra_resource=True)
    original = deepcopy(claimed.selected_sources_json)
    context = await builder.build(claimed, sequence_start=1)
    registry.build_gateway_runtime(context, audit_writer=Mock())
    policy = ToolExecutionPolicy(context.tools)
    for capability in READS:
        tools = [tool for tool in context.tools if tool.capability == capability]
        assert {t.resource_key for t in tools} == {"runner", "other-runner"}
        args = {"purpose": "fixture"} if capability == "mcp.tools/v1" else {"name": "inspect_window", "arguments": {}}
        with pytest.raises(ToolPolicyViolation, match="resource_key"):
            policy.authorize(capability_to_sdk_name(capability), args)
        for key in ("runner", "other-runner"):
            selected = policy.authorize(capability_to_sdk_name(capability), {**args, "resource_key": key})
            assert str(selected.integration_id) == original[key]["integration_id"]
            assert str(selected.binding_id) == original[key]["binding_id"]
    assert claimed.selected_sources_json == original
