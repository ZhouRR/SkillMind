"""MCP 実行前の観測と、秘密を含まない失敗診断を検証する。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from skillmind.agent.context_builder import (
    ContractStore,
    ProductionRunContextBuilder,
    create_run_tool_registry,
)
from skillmind.agent.mcp_lease import McpDesktopBusyError
from skillmind.agent.mcp_tools_source import McpToolsError, _request_failure
from skillmind.agent.tool_diagnostics import safe_tool_diagnostic
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace import WorkspaceManager
from skillmind.integrations.mcp_tools import McpResultError, parse_result
from tests.agent.test_mcp_context import CONTRACTS, _NoopDocumentSource, mcp_claim
from tests.agent.test_mcp_tools_provider import (
    context as context,
)
from tests.agent.test_mcp_tools_provider import (
    database_resource as database_resource,
)
from tests.agent.test_mcp_tools_provider import (
    mcp_context as mcp_context,
)
from tests.agent.test_mcp_tools_provider import (
    provider as provider,
)
from tests.agent.test_mcp_tools_provider import (
    resource as resource,
)
from tests.agent.test_mcp_tools_provider import (
    tools_resource as tools_resource,
)


@pytest.mark.parametrize("reserve", [False, True])
async def test_discovery_exposes_observed_identity_and_explicit_reservation(
    provider, mcp_context, reserve
):
    """起動なしで版と予約を返し、外部専有や環境版を捏造しない。"""
    obj, source, leases = provider
    obj._capability = "mcp.tools/v1"
    lease = {
        "lease_ref": f"mcp-desktop:{'a' * 64}:{mcp_context.run_id}",
        "run_id": str(mcp_context.run_id),
        "status": "HELD_BY_RUN",
        "pending_operation": False,
        "scope": "SKM_ENDPOINT_ONLY",
        "external_exclusivity": "NOT_VERIFIED",
    }
    leases.acquire.return_value = lease
    result = await obj.execute(mcp_context, {"reserve_desktop": reserve})
    schema = ContractStore(CONTRACTS).load("tools/mcp.tools/v1/response.schema.json")
    response = {**result.response, "evidence_refs": ["ev_observation"]}
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response)
    assert response["server"] == {"name": "ExampleMcp", "version": "2.1.0"}
    assert response["binding_ref"] == str(mcp_context.tool.binding_id)
    assert response["desktop"] == (lease if reserve else None)
    assert result.evidence[0].source_locator["desktop"] == response["desktop"]
    assert "environmentVersion" not in json.dumps(response)
    assert leases.acquire.await_count == int(reserve)
    source.call.assert_not_awaited()
    assert obj._binding._bound.await_count == 2


@pytest.mark.parametrize(
    "reason,expected_code",
    [
        ("contract_changed", "invalid_request"),
        ("invalid_arguments", "invalid_request"),
        ("invalid_response", "unavailable"),
        ("transport_unconfirmed", "unavailable"),
    ],
)
async def test_source_failure_has_actionable_classification(
    provider, mcp_context, reason, expected_code
):
    """契約の不一致と通信・結果の異常を区別し、原操作を再送しない。"""
    obj, source, _ = provider
    obj._capability = "mcp.query/v1"
    source.call.side_effect = McpToolsError(reason)
    with pytest.raises(ToolProviderError) as caught:
        await obj.execute(mcp_context, {"name": "inspect_window", "arguments": {"appId": "sample"}})
    assert caught.value.code == expected_code
    assert caught.value.diagnostic["reason"] == reason
    assert caught.value.diagnostic["local_diagnostic_id"] in caught.value.message
    assert caught.value.diagnostic["remote_detail"] == {}
    assert not caught.value.retryable
    assert obj._binding._bound.await_count == 2
    source.call.assert_awaited_once()


async def test_remote_error_redacts_secrets_and_uses_local_correlation(provider, mcp_context):
    """遠端の機密を含む例外本文を捨て、相関 ID と正しい層を保存する。"""
    obj, source, _ = provider
    obj._capability = "mcp.query/v1"
    diagnostic_id = "b" * 32
    source.call.return_value = {
        "is_error": True,
        "content": [
            {
                "type": "text",
                "text": "password=fixture-secret C:/private/sample.exe "
                f"[diagnosticId={diagnostic_id}]",
            }
        ],
    }
    with pytest.raises(ToolProviderError) as caught:
        await obj.execute(mcp_context, {"name": "inspect_window", "arguments": {"appId": "sample"}})
    error = caught.value
    assert error.code == "unavailable"
    assert error.diagnostic["local_diagnostic_id"] in error.message
    assert error.diagnostic["reason"] == "remote_tool_error"
    assert "diagnostic_id" not in error.diagnostic
    assert error.diagnostic["remote_detail"]["content"][0]["text"] == "[redacted]"
    assert "fixture-secret" not in str(error) and "private" not in str(error)


async def test_busy_desktop_and_revoked_error_do_not_publish_metadata(
    provider, mcp_context, tools_resource
):
    """予約できない時と途中撤権時には版・予約・遠端診断を公開しない。"""
    obj, source, leases = provider
    obj._capability = "mcp.tools/v1"
    leases.acquire.side_effect = McpDesktopBusyError("unsafe detail")
    with pytest.raises(ToolProviderError) as caught:
        await obj.execute(mcp_context, {"reserve_desktop": True})
    assert caught.value.diagnostic["reason"] == "desktop_busy"
    source.call.assert_not_awaited()
    obj._binding._bound.side_effect = [(tools_resource, "token"), (tools_resource, "changed")]
    with pytest.raises(ToolProviderError) as revoked:
        await obj.execute(mcp_context, {"reserve_desktop": True})
    assert revoked.value.code == "scope_denied" and revoked.value.diagnostic is None


async def test_cancelled_query_is_not_a_diagnostic_or_recovery(provider, mcp_context):
    """取消しを通常失敗に変換せず、追加 I/O を行わない。"""
    obj, source, _ = provider
    obj._capability = "mcp.query/v1"
    source.call.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await obj.execute(mcp_context, {"name": "inspect_window", "arguments": {"appId": "sample"}})
    assert obj._binding._bound.await_count == 1


@pytest.mark.parametrize(
    "text",
    [
        "[diagnosticId=secret]",
        "[diagnosticId=" + "a" * 33 + "]",
        "[diagnosticId=" + "a" * 32 + "] [diagnosticId=" + "b" * 32 + "]",
    ],
)
def test_untrusted_diagnostic_text_is_not_reflected(text):
    """形式不正または曖昧な相関値を拒否する。"""
    with pytest.raises(McpResultError) as caught:
        parse_result({"is_error": True, "content": [{"type": "text", "text": text}]})
    assert caught.value.local_diagnostic_id not in text
    assert caught.value.remote_detail["content"][0]["text"] == text
    assert safe_tool_diagnostic(
        {"kind": "mcp", "reason": "remote_tool_error", "diagnostic_id": text, "password": "secret"}
    ) == {"kind": "mcp", "reason": "remote_tool_error"}


def test_sdk_exception_group_retains_only_known_classification():
    """task group に包まれた既知分類を復元し、低層本文は捨てる。"""
    error = ExceptionGroup(
        "unsafe detail", [ValueError("credential"), McpToolsError("contract_changed")]
    )
    assert _request_failure(error).reason == "contract_changed"
    assert str(_request_failure(ValueError("credential"))) == "transport_unconfirmed"


@pytest.mark.parametrize("primary", ["mcp.tools/v1", "mcp.query/v1"])
async def test_new_run_context_exposes_model_and_keeps_binding(tmp_path, primary):
    """原接続と権限を保持し、モデル情報は実 prompt まで伝える。"""
    claimed = mcp_claim(primary=primary)
    allowed = list(claimed.permission_snapshot_json["allowed_capabilities"])
    claimed = replace(
        claimed,
        task_snapshot_json={**claimed.task_snapshot_json, "runtime_policy": "skillmind.runtime/v4"},
        permission_snapshot_json={
            **claimed.permission_snapshot_json,
            "allowed_capabilities": allowed,
        },
    )
    frozen = deepcopy(
        (
            claimed.task_snapshot_json,
            claimed.permission_snapshot_json,
            claimed.selected_sources_json,
        )
    )
    registry = create_run_tool_registry(
        ContractStore(CONTRACTS),
        document_source=_NoopDocumentSource(),
        mcp_tools_provider=Mock(),
        mcp_query_provider=Mock(),
        deferred_features_enabled=False,
        mcp_tools_enabled=True,
    )
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(tmp_path / "runs"),
        tool_registry=registry,
        model="test-model",
        deferred_features_enabled=False,
        mcp_tools_enabled=True,
    )
    result = await builder.build(claimed, sequence_start=1)
    tools = {t.capability: t for t in result.tools}
    assert "mcp.call/v1" not in tools
    assert tools["mcp.tools/v1"].binding_id == tools["mcp.query/v1"].binding_id
    assert result.task_brief["runtime_metadata"]["model"] == "test-model"
    assert '"model":"test-model"' in result.prompt
    assert '"wall_timeout_scope":"AGENT_ATTEMPT"' in result.prompt
    schema = ContractStore(CONTRACTS).load("agent-task-brief/v1.schema.json")
    Draft202012Validator(schema).validate(result.task_brief)
    assert (
        claimed.task_snapshot_json,
        claimed.permission_snapshot_json,
        claimed.selected_sources_json,
    ) == frozen


async def test_diagnostic_is_saved_with_original_failed_toolcall(monkeypatch):
    """監査 transaction も診断 ID を保持し、元失敗を成功に変えない。"""
    from skillmind.db.models import ToolCall
    from tests.agent.test_tool_audit_transactions import AuditDatabase

    database = AuditDatabase(monkeypatch)
    lease = await database.start()
    await database.writer.fail(
        lease,
        code="unavailable",
        retryable=False,
        duration_ms=12,
        diagnostic={
            "kind": "mcp",
            "reason": "remote_tool_error",
            "diagnostic_id": "a" * 32,
            "local_diagnostic_id": "c" * 32,
            "remote_detail": {"error": {"code": "BUSY", "password": "must-redact"}},
            "message": "unsafe private body",
        },
    )
    row = database.rows(ToolCall)[0]
    assert row.status == "FAILED" and row.result_json is None
    assert row.error_json == {
        "code": "unavailable",
        "retryable": False,
        "mcp": {"kind": "mcp", "reason": "remote_tool_error", "diagnostic_id": "a" * 32,
                "local_diagnostic_id": "c" * 32,
                "remote_detail": {"error": {"code": "BUSY", "password": "[redacted]"}}},
    }


def test_source_execution_prompt_carries_model_without_mutating_sources():
    """原文実行方式でも実モデルを提示し、Skill を再解釈・上書きしない。"""
    from uuid import uuid4

    from skillmind.agent.domain import RunLimits
    from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
    from tests.skills.test_source_execution import compile_case

    _, manifest = compile_case()
    original = deepcopy(manifest)
    brief = build_agent_task_brief(
        run_id=uuid4(),
        task_snapshot={
            "runtime_policy": "skillmind.runtime/v4",
            "skill_version_id": str(uuid4()),
            "manifest_checksum": "sha256:" + "a" * 64,
            "output_schema_checksum": "sha256:" + "b" * 64,
        },
        manifest=manifest,
        selected_sources={},
        tools=[],
        model="observed-test-model",
        limits=RunLimits(max_turns=20, wall_timeout_seconds=900, max_output_bytes=100000),
    )
    schema = ContractStore(CONTRACTS).load("agent-task-brief/v2.schema.json")
    Draft202012Validator(schema).validate(brief.brief)
    prompt = render_task_brief_prompt(brief.brief, input_json={}, output_schema={})
    assert '"model":"observed-test-model"' in prompt
    assert '"wall_timeout_seconds":900' in prompt
    assert manifest == original
