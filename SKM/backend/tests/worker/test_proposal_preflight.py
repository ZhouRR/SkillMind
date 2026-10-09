"""SQL 観測の誤りを SDK 停止前に返し、修正後だけ原提案の保存へ進む。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from skillmind.agent.claude import ClaudeRuntimeConfiguration, build_claude_agent_options
from skillmind.agent.codex_mcp import CodexToolBridge
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import _change_propose_tool_definition
from skillmind.agent.tool_gateway import ToolRegistry
from skillmind.core.hashing import sha256_hex
from skillmind.effects.inline import InlineEffectResult
from skillmind.effects.postgres_native import SQL_OBSERVATION_MESSAGE, SqlObservationValidationError
from skillmind.runs.domain import LeaseValidationError, RunCancellationRequestedError
from skillmind.worker.proposal_preflight import ProposalPreflight
from skillmind.worker.tool_authority import ToolExecutionAuthority
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry
from tests.agent.test_tool_policy import _database_proposal
from tests.effects.test_native_resource_effects import sql_execution
from tests.runs.test_effect_continuation import receipt
from tests.worker.test_agent_run_executor import _claimed


@pytest.fixture
def setup(tmp_path):
    """本番の control Tool 契約と原生 SQL を持つ隔離 Run を組み立てる。"""
    from pathlib import Path

    contracts = ContractStore(Path(__file__).resolve().parents[3] / "contracts")
    registry = ToolRegistry((_change_propose_tool_definition(contracts),))
    tool = registry.resolve_unbound("change.propose/v1", execution_profile="GUIDED")
    context = replace(
        _context(tmp_path, _registry(CsvIssueProvider())),
        tools=(tool,),
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": [tool.capability, "workspace.read/v1"],
        },
    )
    authority = ToolExecutionAuthority(
        replace(
            _claimed(),
            run_id=context.run_id,
            run_attempt_id=context.run_attempt_id,
            project_id=context.project_id,
            actor_id=context.user_id,
        )
    )
    arguments, *_ = _database_proposal()
    execution = sql_execution()
    arguments.update(
        capability_version=execution.capability_version,
        operation=execution.operation,
        target=execution.target,
        changes=list(execution.changes),
        precondition=execution.precondition,
    )
    service = AsyncMock()
    service.validate_native_sql_proposal.return_value = None
    validator = ProposalPreflight(service, context, authority)
    return registry, tool, context, authority, arguments, service, validator


@pytest.mark.parametrize("sdk", ["codex", "codex-inline", "claude"])
async def test_bad_observation_is_correctable_without_parking_or_effect(setup, sdk):
    """両 SDK と自動承認経路で誤提案を監査し、同じ Agent を停止させない。"""
    registry, tool, context, _, arguments, service, validator = setup
    service.validate_native_sql_proposal.side_effect = [
        SqlObservationValidationError("private"),
        None,
    ]
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    inline = AsyncMock(return_value=None)
    runtime = replace(
        runtime,
        mcp=replace(
            runtime.mcp,
            on_deferred_validation=validator.validate,
            on_inline_effect=inline if sdk == "codex-inline" else None,
        ),
    )
    session = str(uuid4())
    if sdk.startswith("codex"):
        deferred = AsyncMock()
        bridge = CodexToolBridge(context, runtime, on_deferred=deferred)
        bridge.session_id = session
        rejected = await bridge.invoke(tool.sdk_name, arguments, "bad-call")
        assert rejected.isError and SQL_OBSERVATION_MESSAGE in rejected.content[0].text
        assert bridge.accepting and not bridge.parked.is_set() and bridge.deferred is None
        deferred.assert_not_awaited()
        inline.assert_not_awaited()
        arguments["evidence_refs"] = ["ev_corrected"]
        accepted = await bridge.invoke(tool.sdk_name, arguments, "corrected-call")
        assert not accepted.isError and bridge.parked.is_set()
        deferred.assert_awaited_once_with(tool.sdk_name, arguments, "corrected-call", session)
    else:
        options = build_claude_agent_options(
            context,
            mcp_server=runtime.mcp.server,
            configuration=ClaudeRuntimeConfiguration(environment={}),
            deferred_tool_names=runtime.mcp.deferred_tool_names,
            on_deferred_validation=runtime.mcp.on_deferred_validation,
            on_tool_denied=runtime.mcp.on_tool_denied,
        )
        hook = options.hooks["PreToolUse"][0].hooks[0]
        event = {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": tool.sdk_name,
            "tool_input": arguments,
            "tool_use_id": "bad-call",
        }
        rejected = await hook(event, "bad-call", {"signal": None})
        assert rejected["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert rejected["hookSpecificOutput"]["permissionDecisionReason"] == SQL_OBSERVATION_MESSAGE
        arguments["evidence_refs"] = ["ev_corrected"]
        event["tool_use_id"] = "corrected-call"
        accepted = await hook(event, "corrected-call", {"signal": None})
        assert accepted["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert len(writer.denied) == 1 and not writer.completed
    assert writer.denied[0][1] == SQL_OBSERVATION_MESSAGE
    assert service.validate_native_sql_proposal.await_count == 2


async def test_file_proposal_validates_original_bytes_and_tool_identity(setup):
    """file 経由でも提案を展開して同じ検査へ渡し、変更済み file は拒否する。"""
    _, tool, context, authority, arguments, service, validator = setup
    path = context.workspace.cwd / "proposal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(arguments).encode()
    path.write_bytes(raw)
    request = {
        "request_file": "workspace/proposal.json",
        "expected_hash": "sha256:" + sha256_hex(raw),
    }
    assert await validator.validate(tool.sdk_name, request, "original-call", "session") is None
    service.validate_native_sql_proposal.assert_awaited_once_with(
        authority.claimed,
        arguments,
        tool_use_id="original-call",
    )
    path.write_bytes(b"{}")
    error = await validator.validate(tool.sdk_name, request, "changed-call", "session")
    assert error["code"] == "invalid_request"
    assert service.validate_native_sql_proposal.await_count == 1


@pytest.mark.parametrize("sdk", ["codex", "codex-inline", "claude"])
async def test_sdk_file_proposal_reaches_preflight_before_effect_validation(setup, sdk):
    """両 SDK が file envelope を受理し、展開済み提案を検査して原承認経路へ渡す。"""
    registry, tool, context, authority, arguments, service, validator = setup
    path = context.workspace.cwd / "proposal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(arguments).encode()
    path.write_bytes(raw)
    request = {
        "request_file": "workspace/proposal.json",
        "expected_hash": "sha256:" + sha256_hex(raw),
    }
    writer = MemoryAuditWriter()
    runtime = registry.build_gateway_runtime(context, audit_writer=writer)
    inline = AsyncMock(return_value=InlineEffectResult(uuid4(), receipt()))
    runtime = replace(runtime, mcp=replace(
        runtime.mcp, on_deferred_validation=validator.validate,
        on_inline_effect=inline if sdk == "codex-inline" else None,
    ))
    session = str(uuid4())
    if sdk.startswith("codex"):
        deferred = AsyncMock()
        bridge = CodexToolBridge(context, runtime, on_deferred=deferred)
        bridge.session_id = session
        result = await bridge.invoke(tool.sdk_name, request, "file-call")
        assert not result.isError
        deferred.assert_awaited_once_with(tool.sdk_name, request, "file-call", session)
        if sdk == "codex-inline":
            assert bridge.accepting and not bridge.parked.is_set()
            assert json.loads(result.content[0].text)["delivery"] == "INLINE"
            inline.assert_awaited_once_with(request, "file-call", session)
        else:
            assert not bridge.accepting and bridge.parked.is_set()
            assert bridge.deferred[1]["change_proposal_request"] == request
    else:
        options = build_claude_agent_options(
            context, mcp_server=runtime.mcp.server,
            configuration=ClaudeRuntimeConfiguration(environment={}),
            deferred_tool_names=runtime.mcp.deferred_tool_names,
            on_deferred_validation=runtime.mcp.on_deferred_validation,
            on_tool_denied=runtime.mcp.on_tool_denied,
        )
        hook = options.hooks["PreToolUse"][0].hooks[0]
        result = await hook({
            "hook_event_name": "PreToolUse", "session_id": session,
            "tool_name": tool.sdk_name, "tool_input": request, "tool_use_id": "file-call",
        }, "file-call", {"signal": None})
        assert result["hookSpecificOutput"]["permissionDecision"] == "defer"
    assert not writer.denied
    service.validate_native_sql_proposal.assert_awaited_once_with(
        authority.claimed, arguments, tool_use_id="file-call",
    )


@pytest.mark.parametrize("damage", ["capability", "missing", "schema", "boundary", "revision"])
async def test_expanded_file_is_not_allowed_to_bypass_inline_policy(setup, damage):
    """hash が正しくても不正な内側の能力・形状・境界・行 revision は保存前に拒否する。"""
    _, tool, context, _, arguments, service, validator = setup
    if damage == "capability":
        arguments["capability_version"] = "change.propose/v1"
    elif damage == "missing":
        del arguments["capability_version"]
    elif damage == "schema":
        arguments["changes"] = []
    elif damage == "boundary":
        arguments["target"]["project_id"] = str(uuid4())
    else:
        arguments, *_ = _database_proposal()
        arguments["precondition"]["revision"] = "ABSENT"
    path = context.workspace.cwd / "proposal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(arguments).encode()
    path.write_bytes(raw)
    error = await validator.validate(tool.sdk_name, {
        "request_file": "workspace/proposal.json", "expected_hash": "sha256:" + sha256_hex(raw),
    }, "invalid-call", "session")
    assert error["code"] == "invalid_request" and error["retryable"] is False
    service.validate_native_sql_proposal.assert_not_awaited()


@pytest.mark.parametrize("failure", [ValueError, SQLAlchemyError])
async def test_preflight_returns_fixed_messages_without_raw_details(setup, failure):
    """SQL・parameter・driver 本文を診断へ反射しない。"""
    _, tool, _, _, arguments, service, validator = setup
    service.validate_native_sql_proposal.side_effect = failure("private SQL password parameter")
    error = await validator.validate(tool.sdk_name, arguments, "call", "session")
    assert "private" not in json.dumps(error)
    assert error["code"] == ("invalid_request" if failure is ValueError else "unavailable")


@pytest.mark.parametrize(
    "failure", [asyncio.CancelledError, LeaseValidationError, RunCancellationRequestedError]
)
async def test_preflight_does_not_swallow_execution_supervision(setup, failure):
    """取消・lease 失効を修正可能な業務エラーへ変換しない。"""
    _, tool, _, _, arguments, service, validator = setup
    service.validate_native_sql_proposal.side_effect = failure()
    with pytest.raises(failure):
        await validator.validate(tool.sdk_name, arguments, "call", "session")


async def test_revocation_while_validating_discards_the_diagnostic(setup):
    """検査中に所有 scope が閉じたら、診断も後続提案も配送しない。"""
    _, tool, _, authority, arguments, service, validator = setup

    async def revoked(*args, **kwargs):
        """validation await 中の scope 終了を再現する。"""
        authority._active = False
        raise SqlObservationValidationError("private")

    service.validate_native_sql_proposal.side_effect = revoked
    with pytest.raises(LeaseValidationError):
        await validator.validate(tool.sdk_name, arguments, "call", "session")
