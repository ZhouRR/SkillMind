"""製品名に依存しない MCP の権限、Schema、効果確認を検証する。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.effects.mcp_call import call_payload, check_read_back, has_operation_identity
from skillmind.effects.mcp_provider import McpCallProvider
from skillmind.effects.redmine import EffectProviderTransportError
from skillmind.integrations.domain import IntegrationValidationError, normalize_integration_command
from skillmind.integrations.mcp_tools import configured_tool, normalize_catalog, parse_result
from tests.agent.test_mcp_tools import TOOLS, catalog, config, encoded, execution
from tests.integrations.test_readonly_resources import command


@pytest.mark.parametrize("attempt", [1, 2])
async def test_login_control_references_keep_original_identity_and_readback(attempt):
    """コントロール ID を原 Effect に保存し、再 claim は照会だけでログインを再送しない。"""
    from skillmind.integrations.mcp_tools import digest
    from tests.integrations.test_mcp_metadata import login_command

    settings = normalize_integration_command(login_command())
    payload = {
        "arguments": {
            "requestId": "${effect_id}", "appId": "sample", "windowId": str(uuid4()),
            "usernameAutomationId": "username-input", "passwordAutomationId": "login-input",
            "loginButtonAutomationId": "login-button",
        },
        "read_back": {
            "name": "get_step_status",
            "arguments": {"requestId": "${effect_id}"},
            "checks": [
                {"path": "/requestId", "equals": "${effect_id}"},
                {"path": "/status", "equals": "COMPLETED"},
            ],
        },
    }
    run = replace(
        execution("login", attempt=attempt),
        integration_config=settings.config,
        integration_scope=settings.scope,
        changes=({"path": "/call", "action": "SET", "value": payload},),
        precondition={"revision": digest(settings.config["tool_catalog"])},
    )
    source, leases, authorize = AsyncMock(), AsyncMock(), AsyncMock()
    source.call.return_value = encoded(
        {"requestId": str(run.effect_execution_id), "status": "COMPLETED"}
    )
    result = await McpCallProvider(source=source, leases=leases, authorize=authorize).apply(
        run, credential="fixture-token"
    )
    assert [item.args[2] for item in source.call.await_args_list] == (
        ["login", "get_step_status"] if attempt == 1 else ["get_step_status"]
    )
    assert all(
        item.args[3]["requestId"] == str(run.effect_execution_id)
        for item in source.call.await_args_list
    )
    assert result.before.content["arguments"]["passwordAutomationId"] == "login-input"
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert result.replayed is (attempt == 2)
    assert authorize.await_count >= 4
    leases.confirm.assert_awaited_once()


def test_admin_can_authorize_more_than_five_arbitrary_tools():
    """サービス名・版・工具名ではなく管理者の保存した権限で判定する。"""
    item = replace(
        command("mcp"),
        config=config(),
        capabilities=("mcp.tools/v1", "mcp.query/v1"),
        scope={"resource_uris": [], "tool_names": ["get_environment", "read_inventory"]},
    )
    normalized = normalize_integration_command(item)
    assert (
        configured_tool(normalized.config, normalized.scope, "get_environment")["name"]
        == "get_environment"
    )
    value = normalize_catalog(catalog())
    assert len(value["tools"]) > 5
    for name in TOOLS:
        assert configured_tool(config(), {"tool_names": list(TOOLS)}, name)["name"] == name


@pytest.mark.parametrize(
    "permissions", [{}, {"not_discovered": "read"}, {"get_environment": "unknown"}]
)
def test_remote_discovery_cannot_grant_permissions(permissions):
    """未設定・不存在・未知のモードを接続保存時に拒否する。"""
    item = replace(
        command("mcp"),
        config={**config(), "tool_permissions": permissions},
        capabilities=("mcp.tools/v1", "mcp.query/v1"),
        scope={"resource_uris": [], "tool_names": ["get_environment"]},
    )
    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(item)


async def test_unrelated_service_write_uses_approved_reader_and_checks():
    """既知の UI 操作名を使わずに、実 Provider の承認済み payload を回読する。"""
    original = execution("open_application")
    value = {
        "arguments": {"item": "sample", "quantity": 3},
        "read_back": {
            "name": "read_inventory",
            "arguments": {"item": "sample"},
            "checks": [{"path": "/quantity", "equals": 3}],
        },
    }
    run = replace(
        original,
        target={"locator": "update_inventory", "display": "Stock"},
        changes=({"path": "/call", "action": "SET", "value": value},),
    )
    source, leases = AsyncMock(), AsyncMock()
    source.call.side_effect = [encoded({"accepted": True}), encoded({"quantity": 3})]
    result = await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
        run, credential="fixture-token"
    )
    assert [entry.args[2] for entry in source.call.await_args_list] == [
        "update_inventory",
        "read_inventory",
    ]
    assert result.after.content["read_back"] == {"quantity": 3}
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    leases.confirm.assert_awaited_once()


async def test_protocol_success_without_matching_readback_stays_unconfirmed():
    """通信成功だけで Effect を完了・専有解放しない。"""
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({"status": "RUNNING"})
    with pytest.raises(EffectProviderTransportError):
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            execution(), credential="fixture-token"
        )
    leases.confirm.assert_not_awaited()


def test_identity_requires_call_reader_and_result_correlation():
    """回読条件に UUID を書いただけでは原操作の照会権を作らない。"""
    payload = execution().changes[0]["value"]
    assert has_operation_identity(payload)
    assert not has_operation_identity({**payload, "arguments": {}})
    assert not has_operation_identity(
        {**payload, "read_back": {**payload["read_back"], "arguments": {}}}
    )


@pytest.mark.parametrize("value", [True, "1", None])
def test_readback_compares_json_types_and_missing_paths(value):
    """Python の bool/int 同値や欠損を回読一致としない。"""
    with pytest.raises(ValueError):
        check_read_back({"checks": [{"path": "/count", "equals": 1}]}, {"count": value})
    with pytest.raises(ValueError):
        check_read_back({"checks": [{"path": "/count", "equals": None}]}, {})


def test_generic_result_handles_structured_plain_text_and_attachments():
    """構造化出力優先と非 JSON 内容を保ち、URL は取得しない。"""
    assert parse_result(
        {"is_error": False, "structured_content": {"ready": True}, "content": []}
    ) == {"ready": True}
    content = [
        {"type": "text", "text": "ready"},
        {"type": "resource_link", "uri": "https://example.test/evidence"},
    ]
    assert parse_result({"is_error": False, "content": content}) == {"content": content}


def test_readback_permission_cannot_be_bypassed_by_proposal():
    """承認提案でも変更工具を回読として呼ばせない。"""
    run = execution()
    value = run.changes[0]["value"]
    changed = {**value, "read_back": {**value["read_back"], "name": "update_inventory"}}
    with pytest.raises(ValueError):
        call_payload(
            operation="call",
            target=run.target,
            changes=({"path": "/call", "action": "SET", "value": changed},),
            precondition=run.precondition,
            verification=run.verification,
            scope=run.integration_scope,
            config=run.integration_config,
        )


async def test_cancel_pending_effect_requires_original_run_ownership():
    """工具名に依存しない取消でも、別 Run の操作を取消できない。"""
    from skillmind.agent.mcp_lease import McpDesktopBusyError

    original = execution()
    original_id = str(original.effect_execution_id)
    value = {
        "arguments": {"requestId": original_id},
        "cancel_target": original_id,
        "read_back": {
            "name": "get_step_status",
            "arguments": {"requestId": original_id},
            "checks": [
                {"path": "/requestId", "equals": original_id},
                {"path": "/status", "equals": "CANCELLED"},
            ],
        },
    }
    run = replace(
        execution("cancel_step"), changes=({"path": "/call", "action": "SET", "value": value},)
    )
    source, leases = AsyncMock(), AsyncMock()
    leases.require_original_effect.side_effect = McpDesktopBusyError("foreign effect")
    provider = McpCallProvider(source=source, leases=leases, authorize=AsyncMock())
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(run, credential="fixture-token")
    source.call.assert_not_awaited()
    leases.require_original_effect.side_effect = None
    source.call.return_value = encoded({"requestId": original_id, "status": "CANCELLED"})
    await provider.apply(run, credential="fixture-token")
    assert leases.acquire.await_args.kwargs["cancel_target"] == original.effect_execution_id
    assert leases.require_original_effect.await_args.args[:2] == (run.run_id, run.integration_id)
    leases.confirm.assert_awaited_once()


def test_readonly_annotation_is_metadata_not_execution_permission():
    """遠端注釈だけでは認可を作らず、管理者が保存した権限を必須とする。"""
    value = config()
    entry = next(
        tool for tool in value["tool_catalog"]["tools"] if tool["name"] == "get_environment"
    )
    assert entry["read_only_hint"] is True
    value["tool_permissions"] = {}
    with pytest.raises(ValueError):
        configured_tool(value, {"tool_names": ["get_environment"]}, "get_environment")
    entry["read_only_hint"] = "true"
    with pytest.raises(ValueError):
        normalize_catalog(value["tool_catalog"])
