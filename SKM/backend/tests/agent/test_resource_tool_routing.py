"""同類複数接続の選択・SDK 公開・子呼出し・監査を実 Gateway で検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from skillmind.agent.codex_mcp import CodexToolBridge
from skillmind.agent.evidence import EvidenceDraft, _new_tool_call
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    ToolDefinition,
    ToolProviderError,
    ToolRegistry,
)
from skillmind.agent.tool_policy import (
    ToolExecutionPolicy,
    ToolPolicyViolation,
    capability_to_sdk_name,
)
from skillmind.agent.tool_routing import provider_arguments, resource_identity
from skillmind.agent.tool_sequence import ToolSequenceProvider
from skillmind.core.hashing import canonical_json, sha256_hex
from tests.agent.test_tool_gateway import MemoryAuditWriter, _context, _schema

CASES = [
    ("database.read/v2", "postgres", {"table": "public.items", "purpose": "fixture"}),
    ("database.describe/v1", "postgres", {"table": "public.items", "purpose": "fixture"}),
    ("mcp.tools/v1", "mcp", {"purpose": "fixture"}),
    ("mcp.query/v1", "mcp", {"name": "inspect", "arguments": {"resource_key": "business-value"}}),
    ("mcp.read/v1", "mcp", {"uri": "fixture://items", "purpose": "fixture"}),
    (
        "repository.read/v1",
        "git",
        {"revision": "a" * 40, "path": "README.md", "purpose": "fixture"},
    ),
    ("issue.read/v1", "redmine", {"issue_ref": "TICKET-1", "purpose": "fixture"}),
]


class RecordingProvider:
    """実 resource binding と渡された原引数だけを観測し、外部 I/O を行わない。"""

    def __init__(self):
        self.calls = []
        self.rejected = set()

    async def execute(self, context, arguments):
        """原接続を追跡する。返却される実測の文字/型を加工しない。"""
        self.calls.append((context, deepcopy(arguments)))
        if context.tool.resource_key in self.rejected:
            raise ToolProviderError("scope_denied", "Fixture binding is revoked", retryable=False)
        result = {"status": "success", "actual": "hallo\r"}
        return ProviderToolResult(
            result,
            (
                EvidenceDraft(
                    evidence_type="fixture",
                    source_uri=f"fixture://integration/{context.tool.integration_id}",
                    source_locator={"integration_id": str(context.tool.integration_id)},
                    content_hash="sha256:" + sha256_hex(canonical_json(result)),
                    metadata={"tool_resource": {"resource_key": "forged"}},
                ),
            ),
        )


def routed_runtime(
    tmp_path, capability="database.read/v2", provider="postgres", *, count=2, same_integration=False
):
    """versioned 入力契約と実 routing を使い、接続 I/O だけを置き換える。"""
    recording = RecordingProvider()
    definition = ToolDefinition(
        capability=capability,
        description="Read fixture resource",
        request_schema=_schema(f"tools/{capability}/request.schema.json"),
        response_schema={"type": "object", "required": ["status", "actual", "evidence_refs"]},
        error_schema={"type": "object"},
        providers={provider: recording},
        sequence_safe=True,
    )
    sequence = ToolDefinition(
        capability="tool.sequence/v1",
        description="Run fixed reads",
        request_schema=_schema("tools/tool.sequence/v1/request.schema.json"),
        response_schema=_schema("tools/tool.sequence/v1/response.schema.json"),
        error_schema=_schema("tools/tool.sequence/v1/error.schema.json"),
        providers={"platform": ToolSequenceProvider()},
        unbound_provider="platform",
    )
    registry = ToolRegistry((definition, sequence))
    # _context only uses issue.read registry to construct the generic Run workspace.
    from tests.agent.test_tool_gateway import CsvIssueProvider, _registry

    context = _context(tmp_path, _registry(CsvIssueProvider()))
    integration = uuid4()
    tools = tuple(
        registry.resolve(
            capability,
            provider=provider,
            integration_id=integration if same_integration else uuid4(),
            binding_id=uuid4(),
            resource_key=key,
        )
        for key in ("source", "result")[:count]
    )
    context = replace(
        context,
        tools=tools
        + (registry.resolve_unbound("tool.sequence/v1", execution_profile="SUPERVISED"),),
        permission_snapshot={
            "mode": "auto_read_only",
            "execution_profile": "SUPERVISED",
            "allowed_capabilities": [capability, "tool.sequence/v1"],
        },
    )
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    return context, runtime, writer, recording


async def call(context, runtime, arguments, *, capability=None, call_id="call-1", session=None):
    """SDK の認可と dispatch を実際の順序で呼ぶ。"""
    name = capability_to_sdk_name(capability or context.tools[0].capability)
    await runtime.mcp.on_tool_authorized(name, arguments, call_id, session or str(uuid4()))
    response = await runtime.gateway.invoke_mcp(name, arguments)
    return json.loads(response["content"][0]["text"])


@pytest.mark.parametrize("capability,provider,arguments", CASES)
@pytest.mark.parametrize("same_integration", [False, True])
async def test_routes_keep_binding_audit_and_original_values(
    tmp_path, capability, provider, arguments, same_integration
):
    """同名表/工具/path でも明示 slot に届き、同接続の別 scope も混ぜない。"""
    context, runtime, writer, recorded = routed_runtime(
        tmp_path, capability, provider, same_integration=same_integration
    )
    original = deepcopy(arguments)
    for number, key in enumerate(("source", "result")):
        result = await call(
            context, runtime, {**arguments, "resource_key": key}, call_id=f"call-{number}"
        )
        assert result["actual"] == "hallo\r"
        observed, sent = recorded.calls[number]
        assert observed.tool == context.tools[number]
        assert sent == original  # nested business resource_key is preserved
        invocation = list(writer.invocations.values())[number]
        assert invocation.arguments["resource_key"] == key
        assert invocation.tool.integration_id == context.tools[number].integration_id
        assert writer.completed[number][1][0].draft.metadata["tool_resource"] == resource_identity(
            context.tools[number]
        )
    assert arguments == original
    assert len({i.request_fingerprint for i in writer.invocations.values()}) == 2


@pytest.mark.parametrize("selector", [None, "unknown", "Source", "https://outside.example", 3, {}])
async def test_invalid_or_missing_selector_does_not_acquire_execution(tmp_path, selector):
    """曖昧さを先頭接続で補完せず、認可/Provider より前に返す。"""
    context, runtime, writer, recording = routed_runtime(tmp_path)
    args = {"table": "public.items", "purpose": "fixture"}
    if selector is not None:
        args["resource_key"] = selector
    with pytest.raises(ToolPolicyViolation, match="resource_key"):
        await call(context, runtime, args)
    assert not writer.invocations and not recording.calls


@pytest.mark.parametrize("capability,provider,arguments", CASES)
async def test_single_resource_keeps_omission_and_explicit_selection(
    tmp_path, capability, provider, arguments
):
    """既存の一接続呼出しに新必填項目を要求しない。"""
    context, runtime, _, recorded = routed_runtime(tmp_path, capability, provider, count=1)
    await call(context, runtime, arguments)
    await call(context, runtime, {**arguments, "resource_key": "source"}, call_id="call-2")
    assert len(recorded.calls) == 2
    assert recorded.calls[0][0].tool == recorded.calls[1][0].tool


@pytest.mark.parametrize("capability,provider,arguments", CASES)
def test_sdk_advertises_one_tool_and_exact_resource_enum(tmp_path, capability, provider, arguments):
    """Codex/Claude 共通 policy は同名工具を二重公開せず、slot 候補を正確に示す。"""
    context, _, _, _ = routed_runtime(tmp_path, capability, provider)
    policy = ToolExecutionPolicy(context.tools)
    public = [t for t in policy.sdk_tools if t.capability == capability]
    assert len(public) == 1
    schema = public[0].input_schema
    assert schema["properties"]["resource_key"]["enum"] == ["source", "result"]
    assert "resource_key" in schema["required"]
    assert not Draft202012Validator(schema).is_valid(arguments)
    assert Draft202012Validator(schema).is_valid({**arguments, "resource_key": "source"})
    assert len(policy.allowed_sdk_names) == 2
    assert policy.registered(public[0].sdk_name) is None  # no false route on denial


async def test_cross_resource_replay_is_rejected_even_with_broken_audit_double(tmp_path):
    """同じ SDK request ID を別 scope に付け替えた cache 回执を返さない。"""
    context, runtime, _, recording = routed_runtime(tmp_path, same_integration=True)
    args = {"table": "public.items", "purpose": "fixture", "resource_key": "source"}
    session = str(uuid4())
    first = await call(context, runtime, args, session=session)
    second = await call(context, runtime, args, session=session)
    assert first == second and len(recording.calls) == 1
    changed = await call(context, runtime, {**args, "resource_key": "result"}, session=session)
    assert changed["status"] == "error" and len(recording.calls) == 1


async def test_sequence_can_choose_two_resources_with_independent_audit(tmp_path):
    """各子引数の selector を原 Gateway が消費し、違う接続の読取を集約する。"""
    context, runtime, writer, recording = routed_runtime(tmp_path)
    args = {
        "purpose": "fixture",
        "steps": [
            {
                "capability": "database.read/v2",
                "arguments": {"resource_key": key, "table": "public.items", "purpose": "fixture"},
            }
            for key in ("source", "result")
        ],
    }
    result = await call(context, runtime, args, capability="tool.sequence/v1")
    assert result["outcome"] == "COMPLETED"
    assert [c.tool.resource_key for c, _ in recording.calls] == ["source", "result"]
    assert len(writer.completed) == 3
    assert runtime.gateway.step_budget.used == 2  # outer budget is normally consumed by SDK


async def test_sequence_checks_all_selectors_and_stops_on_revocation(tmp_path):
    """後段が曖昧なら前段も実行しない。実行中の撤権ならその後へ進まない。"""
    context, runtime, _, recording = routed_runtime(tmp_path)
    steps = [
        {
            "capability": "database.read/v2",
            "arguments": {"resource_key": key, "table": "public.items", "purpose": "fixture"},
        }
        for key in ("source", "result", "source")
    ]
    invalid = deepcopy(steps)
    del invalid[1]["arguments"]["resource_key"]
    result = await call(
        context, runtime, {"steps": invalid, "purpose": "fixture"}, capability="tool.sequence/v1"
    )
    assert result["status"] == "error" and not recording.calls
    recording.rejected.add("result")
    result = await call(
        context,
        runtime,
        {"steps": steps, "purpose": "fixture"},
        capability="tool.sequence/v1",
        call_id="outer-2",
    )
    assert result["outcome"] == "STOPPED" and result["not_run"] == [2]
    assert len(recording.calls) == 2


def test_multiplex_rejects_unlabelled_duplicates_and_scope_injection(tmp_path):
    """名前の衝突チェックは撤廃せず、正確な resource key のある組だけを許す。"""
    context, _, _, _ = routed_runtime(tmp_path)
    first, second = context.tools[:2]
    for bad in (replace(second, resource_key=None), replace(second, resource_key="source")):
        with pytest.raises(ValueError, match="Duplicate"):
            ToolExecutionPolicy((first, bad))
    for field in ("integration_id", "provider", "credential"):
        with pytest.raises(ToolPolicyViolation):
            ToolExecutionPolicy(context.tools).authorize(
                first.sdk_name,
                {
                    "table": "public.items",
                    "purpose": "fixture",
                    "resource_key": "source",
                    field: "override",
                },
            )


def test_toolcall_summary_preserves_exact_resource_without_credentials(tmp_path):
    """同名能力の ToolCall に元 slot と binding を記録し、後から区別できる。"""
    from datetime import UTC, datetime

    from skillmind.agent.evidence import ToolInvocation, invocation_fingerprint

    context, _, _, _ = routed_runtime(tmp_path)
    tool = context.tools[1]
    args = {"resource_key": "result", "table": "public.items", "purpose": "fixture"}
    invocation = ToolInvocation(
        context.run_id,
        context.run_attempt_id,
        uuid4(),
        "test-call",
        tool,
        args,
        invocation_fingerprint(tool.sdk_name, args),
    )
    row = _new_tool_call(invocation, status="RUNNING", now=datetime.now(UTC))
    assert row.arguments_summary["resource"] == resource_identity(tool)
    assert row.integration_id == tool.integration_id


def test_provider_arguments_remove_only_top_level_resource_key(tmp_path):
    """外部 MCP の入力に同名の業務項目があっても再帰的に消さない。"""
    context, _, _, _ = routed_runtime(tmp_path)
    args = {"resource_key": "source", "arguments": {"resource_key": "application-value"}}
    assert provider_arguments(context.tools[0], args) == {
        "arguments": {"resource_key": "application-value"}
    }
    assert args["resource_key"] == "source"


async def test_codex_bridge_dispatches_same_tool_to_each_explicit_resource(tmp_path):
    """実 bridge の二回呼出しが同一 session/異なる原 binding を保持する。"""
    context, runtime, writer, recording = routed_runtime(tmp_path)
    bridge = CodexToolBridge(context, runtime, on_deferred=AsyncMock())
    bridge.session_id = str(uuid4())
    for key in ("source", "result"):
        response = await bridge.invoke(
            context.tools[0].sdk_name,
            {
                "resource_key": key,
                "table": "public.items",
                "purpose": "fixture",
            },
            f"call-{key}",
        )
        assert not response.isError
    assert len(writer.completed) == 2 and len(recording.calls) == 2
    assert not bridge.parked.is_set()


@pytest.mark.parametrize(
    "kind,provider_name,primary,companions,keyword",
    [
        (
            "other",
            "postgres",
            "database.read/v1",
            ("database.read/v2", "database.describe/v1"),
            "database_provider",
        ),
        ("other", "mcp", "mcp.query/v1", ("mcp.query/v1", "mcp.tools/v1"), "mcp_query_provider"),
        ("repository", "git", "repository.read/v1", ("repository.read/v1",), "repository_source"),
    ],
)
async def test_context_builder_prepares_two_resources_and_every_companion(
    tmp_path, kind, provider_name, primary, companions, keyword
):
    """本番装配から同類二接続を組み立て、Brief と schema/発見の各 binding を照合する。"""
    from unittest.mock import Mock

    from skillmind.agent.context_builder import ProductionRunContextBuilder
    from skillmind.agent.contract_store import ContractStore
    from skillmind.agent.tool_catalog import create_run_tool_registry
    from skillmind.agent.workspace import WorkspaceManager
    from tests.agent.test_runtime_context import (
        CONTRACTS,
        _generic_claimed,
        _generic_manifest,
        _NoopDocumentSource,
    )

    manifest = _generic_manifest()
    capabilities = list(dict.fromkeys((primary, *companions)))
    manifest["tools"] = [{"capability": c, "required": True} for c in capabilities]
    manifest["capability_blueprint"]["resource_requirements"] = [
        {
            "key": key,
            "kind": kind,
            "required": True,
            "access": "read",
            "capabilities": capabilities,
            "accepted_providers": [provider_name],
        }
        for key in ("source", "result")
    ]
    selected = {
        key: {"capability": primary, "provider": provider_name} for key in ("source", "result")
    }
    claimed = _generic_claimed(
        manifest=manifest, allowed=tuple(capabilities), selected_sources=selected
    )
    claimed = replace(
        claimed,
        task_snapshot_json={**claimed.task_snapshot_json, "runtime_policy": "skillmind.runtime/v3"},
    )
    injected = {keyword: Mock()}
    if provider_name == "mcp":
        injected["mcp_tools_provider"] = Mock()
    registry = create_run_tool_registry(
        ContractStore(CONTRACTS),
        document_source=_NoopDocumentSource(),
        deferred_features_enabled=False,
        mcp_tools_enabled=True,
        **injected,
    )
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(tmp_path / "runs"),
        tool_registry=registry,
        model="test-model",
        deferred_features_enabled=False,
        mcp_tools_enabled=True,
    )
    before = deepcopy(claimed.selected_sources_json)
    context = await builder.build(claimed, sequence_start=1)
    runtime = registry.build_gateway_runtime(context, audit_writer=MemoryAuditWriter())
    for capability in companions:
        routes = [t for t in context.tools if t.capability == capability]
        assert len(routes) == 2 and {t.resource_key for t in routes} == {"source", "result"}
        for tool in routes:
            assert str(tool.binding_id) == before[tool.resource_key]["binding_id"]
            assert str(tool.integration_id) == before[tool.resource_key]["integration_id"]
        assert capability_to_sdk_name(capability) in runtime.tool_descriptions
        assert {
            t["resource_key"]
            for t in context.task_brief["allowed_tools"]
            if t["capability"] == capability
        } == {"source", "result"}
    if provider_name == "postgres":
        assert "database.read/v1" not in {t.capability for t in context.tools}
    assert claimed.selected_sources_json == before
    assert all(not obj.mock_calls for obj in injected.values())


async def test_continuation_identity_includes_original_resource_routes(tmp_path):
    """同名工具の schema が同じでも binding 差替えを増分復旧として扱わない。"""
    from skillmind.agent.continuation_prompt import continuation_prompt
    from tests.agent.test_continuation_prompt import context_with_brief

    context = context_with_brief(tmp_path)
    first = replace(context.tools[0], resource_key="source", binding_id=uuid4())
    second = replace(first, resource_key="result", integration_id=uuid4(), binding_id=uuid4())
    context = replace(context, tools=(first, second))
    _, metadata = continuation_prompt(context, context.prompt, None)
    changed = replace(context, tools=(first, replace(second, binding_id=uuid4())))
    prompt, changed_metadata = continuation_prompt(changed, changed.prompt, metadata)
    assert changed_metadata["static_prompt_checksum"] != metadata["static_prompt_checksum"]
    assert prompt == changed.prompt


def test_resource_selection_preserves_original_registered_object(tmp_path):
    """同類の binding を選んでも認可 object を置き換えず、SDK view は元契約を変更しない。"""
    context, _, _, _ = routed_runtime(tmp_path)
    original = deepcopy(context.tools)
    policy = ToolExecutionPolicy(context.tools)
    for tool in context.tools[:2]:
        args = {"resource_key": tool.resource_key, "table": "public.items", "purpose": "fixture"}
        assert policy.authorize(tool.sdk_name, args) is tool
    public = policy.sdk_tools[0]
    public.input_schema["properties"]["resource_key"]["enum"].append("not-authorized")
    assert context.tools == original
    with pytest.raises(ToolPolicyViolation, match="resource_key"):
        policy.authorize(
            context.tools[0].sdk_name,
            {
                "resource_key": "not-authorized",
                "table": "public.items",
                "purpose": "fixture",
            },
        )


def test_unbound_proposal_resource_key_and_identity_are_not_consumed():
    """提案の business selector は原 proposal validator に渡し、外部 write 権を作らない。"""
    from tests.agent.test_tool_policy import _database_proposal

    args, _, tool, policy = _database_proposal()
    original = deepcopy(args)
    assert policy.authorize(tool.sdk_name, args) is tool
    assert provider_arguments(tool, args) == original
    assert policy.registered(tool.sdk_name) is tool
    assert args == original
