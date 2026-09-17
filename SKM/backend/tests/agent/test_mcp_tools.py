"""MCP SDK の実 protocol と承認済み一操作の境界を隔離 transport で検証する。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from skillmind.agent.mcp_source import McpReadError
from skillmind.agent.mcp_tools_source import (
    McpToolsError,
    StreamableHttpMcpToolsSource,
    _ToolTransport,
)
from skillmind.effects.mcp_provider import McpCallProvider
from skillmind.effects.redmine import EffectProviderTransportError
from skillmind.integrations.mcp_tools import (
    PROFILE,
    TOOLS,
    digest,
    normalize_catalog,
    validate_arguments,
)
from tests.agent.test_mcp_source import ENDPOINT, ResourceStream, json_response
from tests.effects.database_fixtures import database_execution


def catalog():
    """外部サービスの住所・説明文を含まない有界 tool fixture。"""
    return normalize_catalog(
        {
            "server": {"name": "FlaUiMcp", "version": "0.3.0.0"},
            "tools": [
                {
                    "name": name,
                    "description": name,
                    "input_schema": {"type": "object"},
                    "output_schema": None,
                }
                for name in TOOLS
            ],
        }
    )


def config():
    """凍結契約を持つ隔離 endpoint。"""
    return {
        "server_url": ENDPOINT,
        "transport": "streamable_http",
        "tool_profile": PROFILE,
        "tool_catalog": catalog(),
    }


def encoded(result):
    """FlaUI の text JSON envelope を作る。"""
    return {"is_error": False, "content": [{"type": "text", "text": json.dumps(result)}]}


class ToolServer(httpx.AsyncBaseTransport):
    """実 SDK を使い、ネットワークと実デスクトップを使わない。"""

    def __init__(self, *, sse=False, changed=False):
        """応答方式と契約変更を固定する。"""
        self.sse, self.changed, self.messages, self.closed = sse, changed, [], False

    async def handle_async_request(self, request):
        """tool 発見と一回の明示 call のみに応答する。"""
        assert str(request.url) == ENDPOINT
        assert request.headers.get("authorization") == "Bearer fixture-token"
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(204)
        message = json.loads(request.content)
        self.messages.append(message)
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": catalog()["server"],
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": t["name"],
                        "description": t["description"] + ("changed" if self.changed else ""),
                        "inputSchema": t["input_schema"],
                    }
                    for t in catalog()["tools"]
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": '{"status":"READY"}'}], "isError": False}
        else:
            return httpx.Response(202)
        response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if self.sse:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "Mcp-Session-Id": "fixture"},
                stream=ResourceStream(response),
            )
        return json_response(response, {"Mcp-Session-Id": "fixture"})

    async def aclose(self):
        """pool close を観測する。"""
        self.closed = True


@pytest.mark.parametrize("sse", [False, True])
async def test_real_sdk_discovers_then_calls_exact_tool_once(sse):
    """JSON/SSE と session cleanup を実 SDK で通し、native tool 登録を要しない。"""
    server = ToolServer(sse=sse)
    source = StreamableHttpMcpToolsSource(transport_factory=lambda: server)
    result = await source.call(config(), "fixture-token", "inspect_window", {"appId": "sample"})
    assert result["is_error"] is False and server.closed
    methods = [m.get("method") for m in server.messages]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert server.messages[-1]["params"] == {
        "name": "inspect_window",
        "arguments": {"appId": "sample"},
    }


async def test_catalog_change_prevents_call_and_error_has_no_body():
    """発見した schema/説明が凍結内容と違えば、送信前に閉じる。"""
    server = ToolServer(changed=True)
    with pytest.raises(McpToolsError):
        await StreamableHttpMcpToolsSource(transport_factory=lambda: server).call(
            config(), "fixture-token", "open_application", {"appId": "sample"}
        )
    assert "tools/call" not in [m.get("method") for m in server.messages]


def test_transport_rejects_second_call_protocol_override_and_resources():
    """SDK 再送も外部 method 追加も同じ送信門で止める。"""
    call = {"name": "open_application", "arguments": {"appId": "sample"}}
    transport = _ToolTransport(ENDPOINT, ToolServer(), call)
    transport._check_rpc({"method": "tools/call", "params": call})
    for payload in [
        {"method": "tools/call", "params": call},
        {"method": "resources/read"},
        {"method": "roots/list"},
    ]:
        with pytest.raises(McpReadError):
            transport._check_rpc(payload)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.test/schema"},
        {"properties": {"a": {"$ref": "#/b"}}},
        {"type": "bad"},
    ],
)
def test_catalog_rejects_external_or_recursive_schema(schema):
    """Schema の解決を network/再帰経路にしない。"""
    value = catalog()
    value["tools"][0]["input_schema"] = schema
    with pytest.raises(ValueError):
        normalize_catalog(value)


def execution(name="execute_step", attempt=1):
    """実 Effect snapshot の型を使い、原 UUID を保持する。"""
    args = (
        {"appId": "sample", "operation": "click", "inputsJson": '{"automationId":"submit"}'}
        if name == "execute_step"
        else {"appId": "sample"}
    )
    return replace(
        database_execution(),
        capability_version="mcp.call/v1",
        provider="mcp",
        operation=name,
        target={"locator": name, "display": name},
        changes=({"path": "/call", "action": "SET", "value": args},),
        integration_config=config(),
        integration_scope={"resource_uris": [], "tool_names": sorted(TOOLS)},
        precondition={"revision": digest(catalog())},
        verification={"method": "READ_BACK", "paths": ["/call"]},
        attempt_no=attempt,
    )


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("state", ["COMPLETED", "ERROR"])
async def test_original_id_readback_and_retry_without_action(attempt, state):
    """操作 ERROR とテスト PASS を混ぜず、再 claim でクリックを再実行しない。"""
    run = execution(attempt=attempt)
    response = encoded(
        {
            "requestId": str(run.effect_execution_id),
            "appId": "sample",
            "operation": "click",
            "status": state,
        }
    )
    source, leases, authorize = AsyncMock(), AsyncMock(), AsyncMock()
    source.call.return_value = response
    result = await McpCallProvider(source=source, leases=leases, authorize=authorize).apply(
        run, credential="fixture-token"
    )
    names = [call.args[2] for call in source.call.await_args_list]
    assert names == (["execute_step", "get_step_status"] if attempt == 1 else ["get_step_status"])
    assert all(
        call.args[3]["requestId"] == str(run.effect_execution_id)
        for call in source.call.await_args_list
    )
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert result.verification["operation_status"] == state
    leases.confirm.assert_awaited_once()
    assert authorize.await_count >= 4


async def test_lost_response_keeps_pending_and_never_replays_launch():
    """応答不明でも lease を解放せず、原 ID のない起動を再送しない。"""
    source, leases = AsyncMock(), AsyncMock()
    source.call.side_effect = McpToolsError("unconfirmed")
    provider = McpCallProvider(source=source, leases=leases, authorize=AsyncMock())
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(execution(), credential="fixture-token")
    assert source.call.await_count == 1
    leases.confirm.assert_not_awaited()
    source.reset_mock()
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(execution("open_application", attempt=2), credential="fixture-token")
    source.call.assert_not_awaited()


async def test_launch_uses_process_readback_and_does_not_claim_pass():
    """起動結果の processId を画面検査と照合する。"""
    source, leases = AsyncMock(), AsyncMock()
    source.call.side_effect = [
        encoded({"status": "READY", "processId": 42}),
        encoded({"status": "READY", "processId": 42}),
    ]
    result = await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
        execution("open_application"), credential="fixture-token"
    )
    assert [c.args[2] for c in source.call.await_args_list] == [
        "open_application",
        "inspect_window",
    ]
    assert result.verification["business_verdict"] == "NOT_EVALUATED"


@pytest.mark.parametrize("error", [PermissionError("revoked"), asyncio.CancelledError()])
async def test_authority_loss_and_cancellation_do_not_contact_server(error):
    """取消を一般通信障害へ変換せず、失効後の外部呼出しを禁止する。"""
    source = AsyncMock()
    with pytest.raises(
        asyncio.CancelledError
        if isinstance(error, asyncio.CancelledError)
        else EffectProviderTransportError
    ):
        await McpCallProvider(
            source=source, leases=AsyncMock(), authorize=AsyncMock(side_effect=error)
        ).apply(execution(), credential="fixture-token")
    source.call.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        {"appId": "sample", "operation": "shell", "inputsJson": "{}"},
        {"appId": "sample", "operation": "click", "inputsJson": '{"automationId":"a","name":"b"}'},
        {
            "appId": "sample",
            "operation": "click",
            "inputsJson": '{"automationId":"a"}',
            "requestId": "00000000-0000-4000-8000-000000000001",
        },
    ],
)
def test_proposal_cannot_choose_request_id_or_unreviewed_actions(args):
    """Tool Schema が緩くても profile の業務境界を通過できない。"""
    with pytest.raises(ValueError):
        validate_arguments("execute_step", args, proposal=True)


async def test_reconciliation_only_queries_original_step_and_never_launches():
    """Write lease 失効後の核対でも原操作を再送せず、起動の因果を捏造しない。"""
    from skillmind.effects.mcp_receipt import McpOperationCommand, lookup_operation
    from skillmind.effects.reconciliation_domain import EffectReconciliationTarget
    from skillmind.effects.reconciliation_requests import reconciliation_kind

    run = execution()
    command = McpOperationCommand(
        run.effect_execution_id,
        run.project_id,
        run.run_id,
        run.integration_id,
        run.operation,
        json.dumps(run.changes[0]["value"]),
    )
    source = AsyncMock()
    source.call.return_value = encoded(
        {
            "requestId": str(run.effect_execution_id),
            "appId": "sample",
            "operation": "click",
            "status": "ERROR",
        }
    )
    receipt = await lookup_operation(source, config(), "fixture-token", command)
    assert receipt.result["status"] == "ERROR"
    assert source.call.await_args.args[2:] == (
        "get_step_status",
        {"requestId": str(run.effect_execution_id)},
    )
    target = EffectReconciliationTarget(
        run.proposal_id,
        run.binding_id,
        "checksum",
        "mcp",
        PROFILE,
        command,
        json.dumps(config()),
        run.secret_reference_id,
    )
    assert reconciliation_kind(target) == "MCP_OPERATION"
    source.reset_mock()
    with pytest.raises(ValueError):
        await lookup_operation(
            source, config(), "fixture-token", replace(command, name="open_application")
        )
    source.call.assert_not_awaited()


async def test_narrowed_binding_without_readback_never_sends_action():
    """Integration に回読権があっても Task scope から除外されていれば外部変更しない。"""
    source = AsyncMock()
    original = execution("open_application")
    restricted = replace(
        original, integration_scope={"resource_uris": [], "tool_names": ["open_application"]}
    )
    with pytest.raises(ValueError):
        await McpCallProvider(source=source, leases=AsyncMock(), authorize=AsyncMock()).apply(
            restricted, credential="fixture-token"
        )
    source.call.assert_not_awaited()
