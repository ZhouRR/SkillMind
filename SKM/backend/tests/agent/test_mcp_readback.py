"""操作前の契約照合と、操作を重放しない有界回読・失敗診断を検証する。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from skillmind.agent.mcp_tools_source import McpToolsError
from skillmind.effects.mcp_call import call_payload
from skillmind.effects.mcp_diagnostics import McpEffectFailure, safe_mcp_diagnostic
from skillmind.effects.mcp_provider import McpCallProvider
from skillmind.integrations.mcp_readback import McpReadBackError, validate_read_back_schema
from skillmind.integrations.mcp_tools import digest
from tests.agent.test_mcp_tools import encoded, execution

CLOSED = {
    "type": "object", "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["READY", "BUSY"]}},
}


@pytest.mark.parametrize("check", [
    {"path": "/appId", "equals": "sample"},
    {"path": "/status", "equals": True},
    {"path": "/status", "one_of": ["UNKNOWN", 1]},
    {"path": "/status/child", "equals": "READY"},
])
def test_declared_output_rejects_missing_fields_and_impossible_values(check):
    """既知の閉じた返却型と矛盾する条件を、操作前に拒否する。"""
    with pytest.raises(McpReadBackError) as error:
        validate_read_back_schema(CLOSED, [check])
    assert error.value.reason == "output_schema_conflict"
    assert error.value.check_index == 0


@pytest.mark.parametrize("schema,path,value", [
    (None, "/appId", "sample"),
    ({"type": "object"}, "/optional", 1),
    ({"anyOf": [CLOSED, {"type": "object", "properties": {"appId": {"type": "string"}}}]},
     "/appId", "sample"),
    ({"type": "array", "items": CLOSED}, "/0/status", "READY"),
    ({"type": "object", "properties": {"a/b": {"type": "integer"}}}, "/a~1b", 1),
    ({"allOf": [CLOSED, {"properties": {"status": {"const": "READY"}}}]}, "/status", "READY"),
])
def test_optional_union_array_and_escaped_paths_remain_usable(schema, path, value):
    """未知 Schema を成功保証にせず、合法な任意属性・union・array を過剰拒否しない。"""
    validate_read_back_schema(schema, [{"path": path, "equals": value}])


@pytest.mark.parametrize("schema,path", [
    ({"allOf": [CLOSED, {"properties": {"status": {"const": "BUSY"}}}]}, "/status"),
    ({"type": "array", "maxItems": 1, "items": CLOSED}, "/1/status"),
])
def test_intersection_and_array_bounds_reject_impossible_readback(schema, path):
    """全枝必須条件と配列上限を無視して承認しない。"""
    with pytest.raises(McpReadBackError):
        validate_read_back_schema(schema, [{"path": path, "equals": "READY"}])


def closed_execution():
    """実在しない appId の返却チェックを含む隔離提案。"""
    original = execution("open_application")
    config = deepcopy(original.integration_config)
    for tool in config["tool_catalog"]["tools"]:
        if tool["name"] == "inspect_window":
            tool["output_schema"] = CLOSED
    value = deepcopy(original.changes[0]["value"])
    value["read_back"]["checks"].append({"path": "/appId", "equals": "sample"})
    return replace(original, integration_config=config,
                   changes=({"path": "/call", "action": "SET", "value": value},),
                   precondition={"revision": digest(config["tool_catalog"])})


async def test_schema_conflict_blocks_both_proposal_and_execution_before_transport():
    """初回提案と claim 後の同じ検証で外部送信を防ぐ。"""
    run = closed_execution()
    with pytest.raises(McpReadBackError):
        call_payload(operation=run.operation, target=run.target, changes=run.changes,
                     precondition=run.precondition, verification=run.verification,
                     scope=run.integration_scope, config=run.integration_config)
    source, leases = AsyncMock(), AsyncMock()
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token")
    assert error.value.code == "mcp_request_not_sent"
    assert error.value.diagnostic["stage"] == "preflight"
    source.call.assert_not_awaited()
    leases.acquire.assert_not_awaited()


@pytest.fixture
def no_delay(monkeypatch):
    """待機時間だけ置き換え、回読の制御は実装を通す。"""
    delay = AsyncMock()
    monkeypatch.setattr("skillmind.effects.mcp_provider.asyncio.sleep", delay)
    return delay


async def test_transient_read_failure_retries_reader_only(no_delay):
    """起動応答後の切断から同じ回読で回復し、起動は一回だけ送る。"""
    source, leases, authorize = AsyncMock(), AsyncMock(), AsyncMock()
    source.call.side_effect = [encoded({"status": "READY"}), McpToolsError(),
                               encoded({"status": "READY"})]
    result = await McpCallProvider(source=source, leases=leases, authorize=authorize).apply(
        execution("open_application"), credential="fixture-token")
    assert [c.args[2] for c in source.call.await_args_list] == [
        "open_application", "inspect_window", "inspect_window"]
    assert result.verification["operation_status"] == "READ_BACK_CONFIRMED"
    leases.confirm.assert_awaited_once()
    no_delay.assert_awaited_once_with(0.5)
    assert authorize.await_count >= 7


async def test_missing_field_stops_without_polling_and_keeps_unverified_responses(no_delay):
    """型の欠損を transient と扱わず、取得済み応答を成功とは別に残す。"""
    run = closed_execution()
    config = deepcopy(run.integration_config)
    for tool in config["tool_catalog"]["tools"]:
        tool["output_schema"] = None
    run = replace(run, integration_config=config,
                  precondition={"revision": digest(config["tool_catalog"])})
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({"status": "READY"})
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token")
    failure = error.value
    assert failure.diagnostic["reason"] == "read_back_path_missing"
    assert failure.diagnostic["check_index"] == 1
    assert failure.diagnostic["call_response_received"] is True
    assert len(failure.observations) == 2
    assert all(o.content["verified"] is False for o in failure.observations)
    assert source.call.await_count == 2
    no_delay.assert_not_awaited()
    leases.confirm.assert_not_awaited()


async def test_remote_failure_preserves_only_fixed_diagnostic_and_bounds_reads(no_delay):
    """例外本文・資格情報を捨て、診断 ID と最初の応答だけを保存する。"""
    source, leases = AsyncMock(), AsyncMock()
    remote = {"is_error": True, "content": [{"type": "text", "text":
              "password=fixture-token [diagnosticId=" + "a" * 32 + "]"}]}
    source.call.side_effect = [encoded({"status": "READY"}), remote, remote, remote]
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            execution("open_application"), credential="fixture-token")
    failure = error.value
    assert failure.diagnostic["stage"] == "read_back"
    assert len(failure.diagnostic["local_diagnostic_id"]) == 32
    assert "diagnostic_id" not in failure.diagnostic
    assert failure.diagnostic["read_back_attempts"] == 3
    assert not failure.retryable
    assert len(failure.observations) == 1
    assert "fixture-token" not in repr(failure.observations) + repr(failure.diagnostic)
    assert [c.args[2] for c in source.call.await_args_list].count("open_application") == 1
    leases.confirm.assert_not_awaited()


@pytest.mark.parametrize("response", [{"password": "hidden"}, {"text": "fixture-token"}])
async def test_sensitive_observation_is_omitted(response, no_delay):
    """秘密を含む成功 envelope も、失敗診断 Evidence へ保存しない。"""
    source = AsyncMock()
    source.call.side_effect = [encoded(response), encoded({})]
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=AsyncMock(), authorize=AsyncMock()).apply(
            execution("open_application"), credential="fixture-token")
    assert error.value.observations[0].content["response"] == {
        "omitted": True, "reason": "sensitive_content"}


async def test_revocation_during_reader_does_not_publish_observations_or_retry(no_delay):
    """回読直後の撤権を retry せず、取得済み本文を返さない。"""
    source, authorize = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({"status": "READY"})
    authorize.side_effect = [None, None, None, None, PermissionError("private")]
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=AsyncMock(), authorize=authorize).apply(
            execution("open_application"), credential="fixture-token")
    assert error.value.diagnostic["reason"] == "authority_revoked"
    assert not error.value.observations
    no_delay.assert_not_awaited()


async def test_cancellation_is_not_converted_to_retry(no_delay):
    """取消しは通常エラーへ変換せず、追加読取・変更を送らない。"""
    source = AsyncMock()
    source.call.side_effect = [encoded({"status": "READY"}), asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await McpCallProvider(source=source, leases=AsyncMock(), authorize=AsyncMock()).apply(
            execution("open_application"), credential="fixture-token")
    no_delay.assert_not_awaited()
    assert source.call.await_count == 2


def test_diagnostic_rejects_malformed_fields_without_reflecting_unknown_data():
    """内部診断にも型検査と白名單を適用する。"""
    valid = {"stage": "verify", "reason": "read_back_mismatch", "action_attempted": True,
             "call_response_received": True, "read_back_attempts": 3}
    assert safe_mcp_diagnostic({**valid, "password": "hidden"}) == valid
    assert safe_mcp_diagnostic({**valid, "stage": []}) is None
    assert safe_mcp_diagnostic({**valid, "read_back_attempts": True}) is None
