"""MCP 原回執の終端確認、送信前の訂正と再照会の受信事実を隔離検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
UPLOAD_OUTPUT = {
    **OUTPUT,
    "properties": {
        "requestId": {"type": "string"},
        "status": {
            "type": "string",
            "enum": ["PREPARED", "PARTIAL", "OUTCOME_UNKNOWN", "UPLOADED"],
            "description": "Only UPLOADED confirms completion; other states require recovery.",
        },
    },
}


def receipt_execution(*, status_check=None, output_schema=OUTPUT, attempt=1, previous_error=None):
    """特定 Runner 名に依存しない、原 ID と終端集合を持つ操作提案を用意する。"""
    run = execution(attempt=attempt)
    config = deepcopy(run.integration_config)
    for tool in config["tool_catalog"]["tools"]:
        if tool["name"] == "execute_step":
            tool["name"] = "submit_job"
        elif tool["name"] == "get_step_status":
            tool["name"], tool["output_schema"] = "lookup_job", output_schema
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
    {"path": "/status", "equals": "UPLOADED"},
    {"path": "/status", "one_of": ["UPLOADED"]},
    {"path": "/status", "one_of": ["UPLOADED", "UPLOADED"]},
])
async def test_single_documented_terminal_state_can_be_proposed_and_confirmed(check):
    """他の enum 値が未完了でも、唯一の保存終端を原 ID で照合して適用できる。"""
    run = receipt_execution(status_check=check, output_schema=UPLOAD_OUTPUT)
    validate_mcp_proposal(proposal(run), run.integration_scope, run.integration_config)
    source, leases = AsyncMock(), AsyncMock()
    receipt = {"requestId": str(run.effect_execution_id), "status": "UPLOADED"}
    source.call.return_value = encoded(receipt)
    result = await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
        run, credential="fixture-token")
    assert result.after.content["read_back"] == receipt
    assert result.verification["operation_status"] == "READ_BACK_CONFIRMED"
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert [call.args[2] for call in source.call.await_args_list] == ["submit_job", "lookup_job"]
    leases.confirm.assert_awaited_once()


async def test_confirmed_preparation_failure_returns_to_agent_without_unknown_or_replay():
    """送信前の確定失敗を原 ID で回読し、保存成功へ変換せず Agent へ返す。"""
    schema = deepcopy(UPLOAD_OUTPUT)
    schema["properties"]["status"]["enum"].append("FAILED")
    schema["properties"].update({
        "errorCode": {"type": "string"}, "message": {"type": "string"},
    })
    run = receipt_execution(
        status_check={"path": "/status", "one_of": ["UPLOADED", "FAILED"]},
        output_schema=schema,
    )
    validate_mcp_proposal(proposal(run), run.integration_scope, run.integration_config)
    receipt = {
        "requestId": str(run.effect_execution_id), "status": "FAILED",
        "errorCode": "PREPARATION_FAILED", "message": "No source records; upload was not sent.",
    }
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded(receipt)
    result = await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
        run, credential="fixture-token",
    )
    assert result.after.content["read_back"] == receipt
    assert result.verification["operation_status"] == "READ_BACK_CONFIRMED"
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert [call.args[2] for call in source.call.await_args_list] == ["submit_job", "lookup_job"]
    leases.confirm.assert_awaited_once()


@pytest.mark.parametrize("status", ["PREPARED", "PARTIAL", "OUTCOME_UNKNOWN"])
async def test_single_terminal_check_does_not_confirm_incomplete_uploads(monkeypatch, status):
    """単一終端の許可で未完了を成功扱いせず、回読だけを再試行する。"""
    monkeypatch.setattr("skillmind.effects.mcp_provider.asyncio.sleep", AsyncMock())
    run = receipt_execution(
        status_check={"path": "/status", "equals": "UPLOADED"}, output_schema=UPLOAD_OUTPUT,
    )
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({
        "requestId": str(run.effect_execution_id), "status": status,
    })
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token")
    assert error.value.diagnostic["reason"] == "read_back_mismatch"
    assert error.value.diagnostic["call_response_received"] is True
    assert [call.args[2] for call in source.call.await_args_list] == [
        "submit_job", "lookup_job", "lookup_job", "lookup_job",
    ]
    leases.confirm.assert_not_awaited()


@pytest.mark.parametrize("status", ["PREPARED", "PARTIAL"])
async def test_documented_partial_receipt_can_be_returned_and_explicitly_continued(status):
    """終了した部分保存を Agent へ返し、別承認の続行でも原リモート ID を維持する。"""
    run = receipt_execution(
        status_check={"path": "/status", "one_of": ["PREPARED", "PARTIAL", "UPLOADED"]},
        output_schema=UPLOAD_OUTPUT,
    )
    validate_mcp_proposal(proposal(run), run.integration_scope, run.integration_config)
    original_id = str(run.effect_execution_id)
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({"requestId": original_id, "status": status})
    provider = McpCallProvider(source=source, leases=leases, authorize=AsyncMock())
    partial = await provider.apply(run, credential="fixture-token")
    assert partial.after.content["read_back"]["status"] == status
    assert partial.verification["business_verdict"] == "NOT_EVALUATED"
    # Provider は自動続行しない。Agent の新規提案が同じ原 ID と対象を明示する。
    assert source.call.await_count == 2
    continuation = replace(run, effect_execution_id=uuid4())
    changes = deepcopy(continuation.changes)
    value = changes[0]["value"]
    value["arguments"]["requestId"] = original_id
    value["read_back"]["arguments"]["requestId"] = original_id
    value["read_back"]["checks"][0]["equals"] = original_id
    continuation = replace(continuation, changes=changes)
    validate_mcp_proposal(
        proposal(continuation), continuation.integration_scope, continuation.integration_config,
    )
    source.call.return_value = encoded({"requestId": original_id, "status": "UPLOADED"})
    completed = await provider.apply(continuation, credential="fixture-token")
    assert completed.after.content["read_back"]["status"] == "UPLOADED"
    assert all(call.args[3] == {"requestId": original_id}
               for call in source.call.await_args_list)
    assert [call.args[2] for call in source.call.await_args_list] == [
        "submit_job", "lookup_job", "submit_job", "lookup_job",
    ]
    assert leases.confirm.await_args_list[0].args[2] == run.effect_execution_id
    assert leases.confirm.await_args_list[1].args[2] == continuation.effect_execution_id


async def test_partial_receipt_contract_still_rejects_unknown_upload_outcomes(monkeypatch):
    """部分完了の受取を許可しても、結果不明を確認済みにせず送信を繰り返さない。"""
    monkeypatch.setattr("skillmind.effects.mcp_provider.asyncio.sleep", AsyncMock())
    run = receipt_execution(
        status_check={"path": "/status", "one_of": ["PREPARED", "PARTIAL", "UPLOADED"]},
        output_schema=UPLOAD_OUTPUT,
    )
    source, leases = AsyncMock(), AsyncMock()
    source.call.return_value = encoded({
        "requestId": str(run.effect_execution_id), "status": "OUTCOME_UNKNOWN",
    })
    with pytest.raises(McpEffectFailure) as error:
        await McpCallProvider(source=source, leases=leases, authorize=AsyncMock()).apply(
            run, credential="fixture-token",
        )
    assert error.value.diagnostic["reason"] == "read_back_mismatch"
    assert [call.args[2] for call in source.call.await_args_list].count("submit_job") == 1
    leases.confirm.assert_not_awaited()


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
                            status_check={"path": "/status", "equals": "UNDECLARED"}
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
def test_enum_membership_does_not_require_accepting_other_statuses(schema, path):
    """入れ子や配列でも enum を終端一覧と解釈せず、合法な比較値を許可する。"""
    validate_read_back_schema(schema, [{"path": path, "equals": "DONE"}])


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
    validate_read_back_schema(schema, [{"path": "/status", "equals": "DONE"}])


def test_resource_state_assertion_is_distinct_from_original_operation_receipt():
    """原 ID の回執ではない通常の状態確認条件は保持する。"""
    validate_read_back_schema(OUTPUT, [{"path": "/status", "equals": "DONE"}])
