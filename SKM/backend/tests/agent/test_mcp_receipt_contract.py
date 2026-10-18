"""MCP 原回執の終端確認、送信前の訂正と再照会の受信事実を隔離検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.mcp_call import validate_mcp_proposal
from skillmind.effects.mcp_diagnostics import McpEffectFailure
from skillmind.effects.mcp_provider import McpCallProvider
from skillmind.effects.outcomes import effect_failure_record
from skillmind.effects.proposal import parse_change_proposal_request
from skillmind.integrations.mcp_readback import McpReadBackError, validate_read_back_schema
from skillmind.integrations.mcp_tools import digest
from tests.agent.test_mcp_tools import encoded, execution

TERMINAL = ["DONE", "REJECTED", "EXPIRED", "STOPPED"]
OUTPUT = {
    "type": "object", "additionalProperties": False,
    "required": ["requestId", "status"],
    "properties": {
        "requestId": {"type": "string"},
        "status": {"type": "string", "enum": ["QUEUED", *TERMINAL]},
    },
}


def receipt_execution(*, status_check=None, attempt=1, previous_error=None):
    """特定 Runner 名に依存しない、原 ID と終端集合を持つ操作提案を用意する。"""
    run = execution(attempt=attempt)
    config = deepcopy(run.integration_config)
    for tool in config["tool_catalog"]["tools"]:
        if tool["name"] == "execute_step":
            tool["name"] = "submit_job"
        elif tool["name"] == "get_step_status":
            tool["name"], tool["output_schema"] = "lookup_job", OUTPUT
    config["tool_permissions"]["submit_job"] = config["tool_permissions"].pop("execute_step")
    config["tool_permissions"]["lookup_job"] = config["tool_permissions"].pop("get_step_status")
    args = {"requestId": "${effect_id}"}
    value = {"arguments": args, "read_back": {
        "name": "lookup_job", "arguments": args,
        "checks": [
            {"path": "/requestId", "equals": "${effect_id}"},
            status_check or {"path": "/status", "one_of": TERMINAL},
        ],
    }}
    return replace(
        run, integration_config=config,
        integration_scope={"tool_names": sorted(config["tool_permissions"])},
        target={"locator": "submit_job", "display": "Submit fixture job"},
        changes=({"path": "/call", "action": "SET", "value": value},),
        precondition={"revision": digest(config["tool_catalog"])},
        previous_diagnostic=(previous_error or {}).get("diagnostic"),
    )


def proposal(run):
    """公開提案を共有 parser へ通し、候補検証の実入口を使う。"""
    return parse_change_proposal_request({
        "resource_key": "runner", "capability_version": run.capability_version,
        "operation": run.operation, "target": run.target, "changes": list(run.changes),
        "precondition": run.precondition, "summary": "Submit a fixture job",
        "evidence_refs": ["ev_fixture_catalog"],
    }, request_identity="fixture-receipt")


@pytest.mark.parametrize("check", [
    {"path": "/status", "equals": "DONE"},
    {"path": "/status", "one_of": ["DONE"]},
    {"path": "/status", "one_of": ["DONE", "DONE"]},
])
async def test_single_receipt_status_is_correctable_before_any_operation(check):
    """提案と実行の両入口で過狭な条件を拒否し、未知の操作を作らない。"""
    run = receipt_execution(status_check=check)
    with pytest.raises(ChangeProposalValidationError, match="all documented terminal states"):
        validate_mcp_proposal(proposal(run), run.integration_scope, run.integration_config)
    source, leases = AsyncMock(), AsyncMock()
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token")
    failure = error.value
    assert failure.code == "mcp_request_not_sent" and not failure.retryable
    assert failure.diagnostic["reason"] == "receipt_status_too_narrow"
    assert failure.diagnostic["check_index"] == 1
    assert failure.diagnostic["action_attempted"] is False
    assert not failure.observations
    source.call.assert_not_awaited()
    leases.acquire.assert_not_awaited()
    record = effect_failure_record(
        capability=run.capability_version, attempt_no=1, code=failure.code,
        retryable=failure.retryable, previous=None, diagnostic=failure.diagnostic,
    )
    assert record["code"] == "mcp_request_not_sent"


@pytest.mark.parametrize("status", TERMINAL)
@pytest.mark.parametrize("attempt", [1, 2])
async def test_unsuccessful_terminal_receipt_is_confirmed_without_business_verdict(status, attempt):
    """成功/失敗の全終端を原回執として返し、書込や業務判定を再生成しない。"""
    run = receipt_execution(attempt=attempt)
    receipt = {"requestId": str(run.effect_execution_id), "status": status}
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded(receipt)
    result = await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
        run, credential="fixture-token")
    assert result.after.content["read_back"] == receipt
    assert result.verification["operation_status"] == "READ_BACK_CONFIRMED"
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert [c.args[2] for c in source.call.await_args_list] == (
        ["submit_job", "lookup_job"] if attempt == 1 else ["lookup_job"])
    leases.confirm.assert_awaited_once()


@pytest.mark.parametrize("mutation", ["pending", "foreign", "missing"])
async def test_identity_alone_or_response_received_never_weakens_approved_checks(
    monkeypatch, mutation,
):
    """非終端・別要求・欠損は未知のまま保持し、元 write は一回しか送らない。"""
    monkeypatch.setattr("skillmind.effects.mcp_provider.asyncio.sleep", AsyncMock())
    run = receipt_execution()
    receipt = {"requestId": str(run.effect_execution_id), "status": "QUEUED"}
    if mutation == "foreign":
        receipt = {"requestId": "another-operation", "status": "REJECTED"}
    elif mutation == "missing":
        receipt.pop("status")
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded(receipt)
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token")
    failure = error.value
    assert failure.diagnostic["call_response_received"] is True
    assert [c.args[2] for c in source.call.await_args_list].count("submit_job") == 1
    leases.confirm.assert_not_awaited()
    record = effect_failure_record(
        capability=run.capability_version, attempt_no=1, code=failure.code,
        retryable=failure.retryable, previous=None, diagnostic=failure.diagnostic,
    )
    assert record["code"] == "effect_result_unknown"


async def test_readonly_retry_inherits_received_response_from_persisted_failure(monkeypatch):
    """初回受信後の未確認を永続形式で引き継ぎ、後続は照会だけで受信事実を保持する。"""
    monkeypatch.setattr("skillmind.effects.mcp_provider.asyncio.sleep", AsyncMock())
    run = receipt_execution()
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({
        "requestId": str(run.effect_execution_id), "status": "QUEUED",
    })
    provider = McpCallProvider(source=source, leases=leases, authorize=AsyncMock())
    previous = None
    for attempt in (1, 2, 3):
        with pytest.raises(McpEffectFailure) as error:
            await provider.apply(replace(run, attempt_no=attempt,
                                         previous_diagnostic=(previous or {}).get("diagnostic")),
                                 credential="fixture-token")
        assert error.value.diagnostic["call_response_received"] is True
        previous = effect_failure_record(
            capability=run.capability_version, attempt_no=attempt, code=error.value.code,
            retryable=error.value.retryable, previous=previous, diagnostic=error.value.diagnostic,
        )
    assert [c.args[2] for c in source.call.await_args_list].count("submit_job") == 1
    assert [c.args[2] for c in source.call.await_args_list].count("lookup_job") == 9
    leases.confirm.assert_not_awaited()


@pytest.mark.parametrize("received", [False, True])
@pytest.mark.parametrize("failure_stage", ["authorize", "preflight", "read_back"])
async def test_retry_diagnostics_retain_only_proven_prior_response(received, failure_stage):
    """再 attempt 数を受信証拠にせず、早期失敗と失権でも以前の事実だけを保持する。"""
    previous = {"diagnostic": {
        "stage": "verify", "reason": "read_back_mismatch", "action_attempted": True,
        "call_response_received": received, "read_back_attempts": 3,
    }}
    run = receipt_execution(attempt=2, previous_error=previous,
                            status_check={"path": "/status", "equals": "DONE"}
                            if failure_stage == "preflight" else None)
    source, authorize = AsyncMock(), AsyncMock()
    if failure_stage == "authorize":
        authorize.side_effect = PermissionError("fixture-revocation")
    else:
        source.call.return_value = encoded({})
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=AsyncMock(), authorize=authorize).apply(
            run, credential="fixture-token")
    assert error.value.diagnostic["call_response_received"] is received
    if failure_stage != "read_back":
        source.call.assert_not_awaited()
    if failure_stage == "authorize":
        assert not error.value.observations


async def test_post_call_revocation_records_receipt_arrival_but_exposes_no_content():
    """応答を受けた直後の失権では、受信の真偽だけを残して本文を公開しない。"""
    source, authorize = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({"private": "fixture-data"})
    authorize.side_effect = [None, None, PermissionError("fixture-revocation")]
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=AsyncMock(), authorize=authorize).apply(
            receipt_execution(), credential="fixture-token")
    assert error.value.diagnostic["call_response_received"] is True
    assert not error.value.observations
    assert "fixture-data" not in repr(error.value.diagnostic)


def test_comparison_candidates_must_all_be_permitted_by_output_schema():
    """一つの合法値を混ぜても、不正な候補を承認条件へ持ち込めない。"""
    with pytest.raises(McpReadBackError, match="output_schema_conflict"):
        validate_read_back_schema(OUTPUT, [{"path": "/status", "one_of": ["DONE", "UNDECLARED"]}])


@pytest.mark.parametrize("schema,path", [
    (OUTPUT, "/status"),
    ({"type": "object", "properties": {"receipt": OUTPUT}}, "/receipt/status"),
    ({"type": "array", "items": OUTPUT}, "/0/status"),
])
def test_receipt_enum_guard_handles_nested_and_array_output(schema, path):
    """外部工具名を見ず、指定 path に宣言された状態候補だけを調べる。"""
    with pytest.raises(McpReadBackError, match="receipt_status_too_narrow"):
        validate_read_back_schema(schema, [{"path": path, "equals": "DONE"}],
                                  operation_receipt=True)


@pytest.mark.parametrize("schema", [
    None,
    {"type": "object", "properties": {"status": {"type": "string"}}},
    {"type": "object", "properties": {"status": {"const": "DONE"}}},
    {"allOf": [OUTPUT, {"properties": {"status": {"const": "DONE"}}}]},
    {**OUTPUT, "const": {"requestId": "fixture", "status": "DONE"}},
    {**OUTPUT, "not": {"properties": {"status": {"enum": ["QUEUED", *TERMINAL[1:]]}}}},
])
def test_preflight_never_invents_other_states_or_rejects_a_fixed_status(schema):
    """状態の宣言がない場合や単一状態への絞込みを、想像した終端 enum で拡張しない。"""
    validate_read_back_schema(schema, [{"path": "/status", "equals": "DONE"}],
                              operation_receipt=True)


def test_resource_state_assertion_is_distinct_from_original_operation_receipt():
    """原 ID の回執ではない通常の状態確認条件は保持する。"""
    validate_read_back_schema(OUTPUT, [{"path": "/status", "equals": "DONE"}])
