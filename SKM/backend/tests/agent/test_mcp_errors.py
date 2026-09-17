"""異なる MCP の標準失敗、診断保護、起動回执を実デスクトップなしで検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData
from skillmind.agent.mcp_tools_provider import _read_error
from skillmind.agent.mcp_tools_source import (
    McpToolsError,
    StreamableHttpMcpToolsSource,
    _request_failure,
)
from skillmind.agent.tool_diagnostics import safe_tool_diagnostic
from skillmind.core.hashing import canonical_json
from skillmind.effects.mcp_diagnostics import McpEffectFailure, diagnostic_message
from skillmind.effects.mcp_provider import McpCallProvider
from skillmind.integrations.mcp_errors import safe_remote_detail
from skillmind.integrations.mcp_tools import McpResultError, parse_result
from tests.agent.test_mcp_source import json_response
from tests.agent.test_mcp_tools import ToolServer, config, encoded, execution


@pytest.mark.parametrize("detail", [
    {"content": [{"type": "text", "text": "No active window"}]},
    {"content": [{"type": "text", "text":
        "[code=SESSION_MISMATCH, stage=process_revalidation, diagnosticId=remote-42]"}]},
    {"structured_content": {"error": {"code": "BUSY", "traceId": "vendor-17"}}},
])
def test_standard_error_preserves_vendor_data_without_parsing_its_format(detail):
    """遠端 ID がなくても分類でき、サービス固有キーは判断規則にしない。"""
    with pytest.raises(McpResultError) as caught:
        parse_result({"is_error": True, **detail, "local_diagnostic_id": "a" * 32})
    result = _read_error(caught.value, "result")
    assert result.diagnostic["reason"] == "remote_tool_error"
    assert result.diagnostic["local_diagnostic_id"] == "a" * 32
    for key, value in detail.items():
        assert result.diagnostic["remote_detail"][key] == value
    assert "untrusted" in result.message
    assert result.retryable is False
    assert safe_tool_diagnostic(result.diagnostic) == result.diagnostic


def test_diagnostics_redact_before_truncation_and_bound_nested_payloads():
    """実 credential、機密代入、添付相当の過大値が結果とログへ漏れない。"""
    detail = safe_remote_detail({
        "message": "x" * 900 + "fixture-credential",
        "error": {"password": "hidden", "reason": "Not ready"},
        "headers": "Authorization: Bearer hidden",
        "url": "https://user:hidden@example.invalid/",
        "items": ["x" * 10000] * 1000,
    }, credential="fixture-credential")
    assert len(canonical_json(detail).encode()) <= 4096
    assert "hidden" not in canonical_json(detail)
    assert "fixture-credential" not in canonical_json(detail)
    small = safe_remote_detail({"error": {"password": "hidden", "reason": "Not ready",
                                          "apiKey": "hidden", "PASSWORD": "hidden"}})
    assert small["error"]["reason"] == "Not ready"
    assert small["error"]["password"] == "[redacted]"
    assert "hidden" not in repr(small)


@pytest.mark.parametrize("code", [408, CONNECTION_CLOSED])
def test_sdk_local_disconnect_and_timeout_are_not_remote_protocol_errors(code):
    """SDK が生成する通信失敗を、遠端 JSON-RPC 拒否と混同しない。"""
    failure = _request_failure(McpError(ErrorData(code=code, message="private")))
    assert failure.reason == "transport_unconfirmed"
    assert failure.remote_detail == {}


class ErrorServer(ToolServer):
    """実 SDK へ JSON-RPC エラーを返す隔離 transport。"""

    async def handle_async_request(self, request):
        """発見は既存 fixture を使い、工具呼出しだけを一回拒否する。"""
        if request.method == "POST":
            message = json.loads(request.content)
            if message.get("method") == "tools/call":
                self.messages.append(message)
                return json_response({"jsonrpc": "2.0", "id": message["id"], "error": {
                    "code": -32602, "message": "Check the selected window",
                    "data": {"requestRef": "server-12", "password": "fixture-token"},
                }}, {"Mcp-Session-Id": "fixture"})
        return await super().handle_async_request(request)


async def test_real_sdk_protocol_error_keeps_safe_detail_without_retry():
    """SDK/task group を通した失敗も分類と SKM 相関を維持する。"""
    server = ErrorServer()
    source = StreamableHttpMcpToolsSource(transport_factory=lambda: server)
    with pytest.raises(McpToolsError) as caught:
        await source.call(config(), "fixture-token", "inspect_window", {"appId": "sample"})
    failure = caught.value
    assert failure.reason == "protocol_error"
    assert failure.remote_detail["code"] == -32602
    assert failure.remote_detail["data"]["requestRef"] == "server-12"
    assert "fixture-token" not in repr(failure.remote_detail)
    assert len(failure.local_diagnostic_id) == 32
    assert sum(m.get("method") == "tools/call" for m in server.messages) == 1


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("status", ["READY", "ERROR", "TIMEOUT", "CANCELLED", "ABORTED"])
async def test_startup_uses_original_receipt_and_does_not_claim_current_screen(attempt, status):
    """起動の各終端を原 ID で確定し、再 claim では起動せず画面確認を別操作に残す。"""
    run = execution("open_application", attempt=attempt)
    changes = deepcopy(run.changes)
    value = changes[0]["value"]
    value["arguments"]["requestId"] = "${effect_id}"
    value["read_back"] = {
        "name": "get_step_status", "arguments": {"requestId": "${effect_id}"},
        "checks": [{"path": "/requestId", "equals": "${effect_id}"},
                   {"path": "/appId", "equals": "sample"},
                   {"path": "/status",
                    "one_of": ["READY", "ERROR", "TIMEOUT", "CANCELLED", "ABORTED"]}],
    }
    run = replace(run, changes=changes)
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({
        "requestId": str(run.effect_execution_id), "appId": "sample", "status": status,
    })
    result = await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
        run, credential="fixture-token")
    assert [c.args[2] for c in source.call.await_args_list] == (
        ["open_application", "get_step_status"] if attempt == 1 else ["get_step_status"])
    assert all(c.args[3]["requestId"] == str(run.effect_execution_id)
               for c in source.call.await_args_list)
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    leases.confirm.assert_awaited_once()


async def test_remote_error_is_audited_without_action_replay_or_logging_body(caplog):
    """遠端本文を権限後に診断へ保存し、ログは関連 ID と分類だけにする。"""
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = {
        "is_error": True, "local_diagnostic_id": "c" * 32,
        "structured_content": {"error": {"code": "OTHER_VENDOR", "message": "Not ready"}},
    }
    run = execution()
    with pytest.raises(McpEffectFailure) as caught:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token")
    diagnostic = caught.value.diagnostic
    assert diagnostic["local_diagnostic_id"] == "c" * 32
    assert "OTHER_VENDOR" in diagnostic_message(diagnostic)
    source.call.assert_awaited_once()
    leases.confirm.assert_not_awaited()
    record = next(r for r in caplog.records if r.message == "mcp.effect.failed")
    assert record.skillmind_context["effect_execution_id"] == str(run.effect_execution_id)
    assert record.skillmind_context["local_diagnostic_id"] == "c" * 32
    assert "OTHER_VENDOR" not in repr(record.__dict__)
