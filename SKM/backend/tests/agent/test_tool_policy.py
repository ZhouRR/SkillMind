"""登録済み Tool の名前、Schema、Run boundary による hard deny を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from skillmind.agent.domain import RegisteredTool
from skillmind.agent.tool_policy import (
    ToolExecutionPolicy,
    ToolPolicyViolation,
    capability_to_sdk_name,
)
from skillmind.effects.database_write import (
    database_row_revision,
    validate_database_write_proposal,
)
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.proposal import (
    CHANGE_PROPOSE_CAPABILITY,
    CHANGE_PROPOSE_REQUEST_SCHEMA,
    CHANGE_PROPOSE_SDK_NAME,
    parse_change_proposal_request,
)

ISSUE_READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["issue_ref", "purpose"],
    "properties": {
        "issue_ref": {"type": "string", "minLength": 1},
        "purpose": {"type": "string", "minLength": 1},
    },
}


def _issue_tool() -> RegisteredTool:
    """Test 用の Redmine 非依存 issue.read binding を返す。"""

    return RegisteredTool(
        capability="issue.read/v1",
        sdk_name="mcp__skillmind__issue_read_v1",
        provider="redmine",
        integration_id=uuid4(),
        input_schema=ISSUE_READ_SCHEMA,
    )


def test_registered_tool_with_valid_input_is_allowed() -> None:
    """Run に登録され Schema を満たす read-only Tool だけが許可される。"""

    policy = ToolExecutionPolicy((_issue_tool(),))

    tool = policy.authorize(
        "mcp__skillmind__issue_read_v1",
        {"issue_ref": "ISSUE-1234", "purpose": "品質分析"},
    )

    assert tool.capability == "issue.read/v1"
    assert policy.allowed_sdk_names == ("mcp__skillmind__issue_read_v1",)


def test_unregistered_tool_is_denied() -> None:
    """allowed_tools に似た名前でも Run 未登録なら実行前に拒否する。"""

    policy = ToolExecutionPolicy((_issue_tool(),))

    with pytest.raises(ToolPolicyViolation, match="not registered"):
        policy.authorize("Bash", {"command": "pwd"})


def test_schema_violation_is_denied() -> None:
    """追加 field を使った Provider や scope のすり替えを Schema で拒否する。"""

    policy = ToolExecutionPolicy((_issue_tool(),))

    with pytest.raises(ToolPolicyViolation, match="registered schema"):
        policy.authorize(
            "mcp__skillmind__issue_read_v1",
            {"issue_ref": "ISSUE-1234", "purpose": "品質分析", "unexpected": True},
        )


def test_nested_boundary_argument_is_denied_before_schema_validation() -> None:
    """入れ子 field でも Integration を model 入力から変更できないことを保証する。"""

    policy = ToolExecutionPolicy((_issue_tool(),))

    with pytest.raises(ToolPolicyViolation, match="integration_id"):
        policy.authorize(
            "mcp__skillmind__issue_read_v1",
            {
                "issue_ref": "ISSUE-1234",
                "purpose": "品質分析",
                "options": {"integration_id": str(uuid4())},
            },
        )


def _database_read_tool(version: str) -> RegisteredTool:
    """実契約を使い、業務 filter と固定接続の境界を検証する。"""
    root = Path(__file__).resolve().parents[3] / "contracts"
    capability = f"database.read/{version}"
    return RegisteredTool(
        capability=capability, sdk_name=capability_to_sdk_name(capability),
        provider="postgres", integration_id=uuid4(), binding_id=uuid4(),
        input_schema=json.loads((root / f"tools/{capability}/request.schema.json").read_text()),
    )


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("column", ["project_id", "integration_id", "provider", "cwd"])
def test_database_filter_columns_preserve_query_and_frozen_connection(version, column):
    """同名の業務列を落とさず通し、モデル値を接続・Run identity に採用しない。"""
    tool = _database_read_tool(version)
    policy = ToolExecutionPolicy((tool,))
    arguments = {"table": "public.reports", "purpose": "Review",
                 "filters": {column: "business-reference"}}
    original = deepcopy(arguments)
    assert policy.authorize(tool.sdk_name, arguments) is tool
    assert arguments == original


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("extra", [
    {"project_id": "other-project"},
    {"options": {"integration_id": "other-connection"}},
])
def test_database_filter_exception_does_not_allow_control_overrides(version, extra):
    """filters 外の境界変更は、業務 filter が合法でも引き続き拒否する。"""
    tool = _database_read_tool(version)
    with pytest.raises(ToolPolicyViolation, match="run boundary"):
        ToolExecutionPolicy((tool,)).authorize(tool.sdk_name, {
            "table": "public.reports", "purpose": "Review",
            "filters": {"project_id": "business-reference"}, **extra,
        })


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("filters", [
    {"project_id": {"integration_id": "other-connection"}},
    {"project_id": ["one", "two"]}, {"invalid.column": "value"},
])
def test_database_filters_still_require_scalar_values_and_valid_column_names(version, filters):
    """境界検査の投影で原 filters の Schema 検証を省略しない。"""
    tool = _database_read_tool(version)
    with pytest.raises(ToolPolicyViolation, match="registered schema"):
        ToolExecutionPolicy((tool,)).authorize(tool.sdk_name, {
            "table": "public.reports", "purpose": "Review", "filters": filters,
        })


def test_other_tools_do_not_inherit_database_filter_exception():
    """別能力の filters に隠した境界引数は database の例外を使えない。"""
    with pytest.raises(ToolPolicyViolation, match="project_id"):
        ToolExecutionPolicy((_issue_tool(),)).authorize("mcp__skillmind__issue_read_v1", {
            "issue_ref": "ISSUE-1234", "purpose": "Review",
            "filters": {"project_id": "other-project"},
        })


@pytest.mark.parametrize("capability", ["issue.read", "Issue.read/v1", "issue.read/v0"])
def test_capability_name_requires_explicit_version(capability: str) -> None:
    """Tool capability ID に明示 version がない場合は SDK 名へ変換しない。"""

    with pytest.raises(ValueError, match="Invalid versioned capability"):
        capability_to_sdk_name(capability)


def _database_proposal(*, column="project_id", operation="INSERT"):
    """公開例から汎用 DB 行の提案を作り、Agent には read/propose だけを許可する。"""

    root = Path(__file__).resolve().parents[3] / "contracts"
    arguments = json.loads((root / "examples/change-propose-request.v1.json").read_text())
    values = {column: "business-reference", "details": {column: "nested-business-reference"}}
    expected = None if operation == "INSERT" else {"id": "record-1", **values}
    arguments.update(
        effect_intent_key="save_record", resource_key="records",
        capability_version="database.write/v1", operation=operation,
        target={"locator": "example.reviews", "display": "Reviewed record"},
        changes=[{
            "path": "/row", "action": "SET",
            "value": {"key": {"id": "record-1"}, "values": values, "expected": expected},
        }],
        precondition={"revision": database_row_revision(expected)},
        verification={"method": "READ_BACK", "paths": ["/row"]},
    )
    scope = {
        "tables": ["example.reviews"], "operations": [operation],
        "write_columns": [f"example.reviews.{key}" for key in ("id", column, "details")],
    }
    tool = RegisteredTool(
        capability=CHANGE_PROPOSE_CAPABILITY, sdk_name=CHANGE_PROPOSE_SDK_NAME,
        provider="platform", integration_id=None, input_schema=CHANGE_PROPOSE_REQUEST_SCHEMA,
    )
    policy = ToolExecutionPolicy(
        (tool,), allowed_capabilities=frozenset({CHANGE_PROPOSE_CAPABILITY, "database.read/v1"}),
    )
    return arguments, scope, tool, policy


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
@pytest.mark.parametrize("column", ["project_id", "integration_id", "provider", "cwd"])
def test_business_columns_pass_real_proposal_validation_without_changing_run_authority(
    column, operation,
) -> None:
    """業務行/原行/JSON 列の名称は制御引数ではなく、原 binding と提案内容を変更しない。"""

    arguments, scope, tool, policy = _database_proposal(column=column, operation=operation)
    original = deepcopy(arguments)
    assert policy.authorize(tool.sdk_name, arguments) is tool
    payload = validate_database_write_proposal(
        parse_change_proposal_request(arguments), binding_scope=scope,
    )
    assert payload["values"][column] == "business-reference"
    assert payload["expected"] == arguments["changes"][0]["value"]["expected"]
    assert arguments == original
    assert tool.integration_id is None and tool.binding_id is None and tool.provider == "platform"
    assert policy.allowed_sdk_names == (CHANGE_PROPOSE_SDK_NAME,)


def test_business_primary_key_is_also_data() -> None:
    """業務主キーが platform と同名でも、凍結 table の行キーとしてのみ検証する。"""

    arguments, scope, tool, policy = _database_proposal()
    row = arguments["changes"][0]["value"]
    row["key"] = {"project_id": row["values"].pop("project_id")}
    policy.authorize(tool.sdk_name, arguments)
    payload = validate_database_write_proposal(
        parse_change_proposal_request(arguments), binding_scope=scope,
    )
    assert payload["key"] == {"project_id": "business-reference"}


def test_invalid_database_row_is_correctable_before_deferred_proposal() -> None:
    """余分な revision は Tool 段階で拒否し、修正後だけ既存の保存検証へ渡す。"""

    arguments, scope, tool, policy = _database_proposal()
    arguments["changes"][0]["value"]["revision"] = "absent"
    original = deepcopy(arguments)
    with pytest.raises(ToolPolicyViolation, match=r"put revision only in precondition\.revision"):
        policy.authorize(tool.sdk_name, arguments)
    assert arguments == original
    del arguments["changes"][0]["value"]["revision"]
    assert policy.authorize(tool.sdk_name, arguments) is tool
    assert validate_database_write_proposal(
        parse_change_proposal_request(arguments), binding_scope=scope,
    )["operation"] == "INSERT"


@pytest.mark.parametrize(
    "capability", ["change.propose/v1", "database.read/v1", "unknown.write/v1"],
)
def test_non_effect_capability_is_correctable_before_deferred_proposal(capability: str) -> None:
    """Tool 名や未登録能力を延期せず、同じ候補の明示的な修正だけを受理する。"""

    arguments, _scope, tool, policy = _database_proposal()
    arguments["capability_version"] = capability
    original = deepcopy(arguments)
    with pytest.raises(ToolPolicyViolation, match="capability_version must name"):
        policy.authorize(tool.sdk_name, arguments)
    assert arguments == original
    arguments["capability_version"] = "database.write/v1"
    assert policy.authorize(tool.sdk_name, arguments) is tool


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
def test_inconsistent_database_revision_is_correctable_before_deferral(operation: str) -> None:
    """不在表記や行摘要の誤りを model に返し、原行から一致する候補だけを延期する。"""

    arguments, scope, tool, policy = _database_proposal(operation=operation)
    arguments["precondition"]["revision"] = "ABSENT"
    with pytest.raises(ToolPolicyViolation, match="revision differs"):
        policy.authorize(tool.sdk_name, arguments)
    arguments["precondition"]["revision"] = database_row_revision(
        arguments["changes"][0]["value"]["expected"],
    )
    assert policy.authorize(tool.sdk_name, arguments) is tool
    assert validate_database_write_proposal(
        parse_change_proposal_request(arguments), binding_scope=scope,
    )["operation"] == operation


@pytest.mark.asyncio
async def test_claude_corrected_row_can_defer_after_validation_denial(tmp_path: Path) -> None:
    """SDK hook は不正な提案で停止を要求せず、修正された同じ入力だけを延期する。"""

    from skillmind.agent.claude import ClaudeRuntimeConfiguration, build_claude_agent_options
    from tests.agent.test_claude_agent_sdk import _run_context

    arguments, _scope, tool, _policy = _database_proposal()
    context = replace(
        _run_context(tmp_path), tools=(tool,),
        permission_snapshot={
            "mode": "auto_read_only", "allowed_capabilities": [CHANGE_PROPOSE_CAPABILITY],
        },
    )
    options = build_claude_agent_options(
        context,
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
        deferred_tool_names=frozenset({tool.sdk_name}),
    )
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    data = {
        "hook_event_name": "PreToolUse", "session_id": "session-1",
        "transcript_path": "/tmp/transcript", "cwd": str(options.cwd),
        "tool_name": tool.sdk_name, "tool_input": arguments, "tool_use_id": "call-1",
    }
    arguments["changes"][0]["value"]["revision"] = "absent"
    denied = await hook(data, "call-1", {"signal": None})  # type: ignore[arg-type]
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "precondition.revision" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    del arguments["changes"][0]["value"]["revision"]
    corrected = await hook(data, "call-2", {"signal": None})  # type: ignore[arg-type]
    assert corrected["hookSpecificOutput"]["permissionDecision"] == "defer"


@pytest.mark.parametrize("field", ["project_id", "integration_id", "provider", "cwd"])
@pytest.mark.parametrize("location", [
    "root", "target", "precondition", "checkpoint", "change", "nested", "invalid_changes",
])
def test_proposal_control_fields_still_reject_boundary_changes(field, location) -> None:
    """正確な業務 value 以外の制御構造では、入れ子や不正 changes も元通り拒否する。"""

    arguments, _scope, tool, policy = _database_proposal()
    if location == "root":
        arguments[field] = "other-boundary"
    elif location == "change":
        arguments["changes"][0][field] = "other-boundary"
    elif location == "nested":
        arguments["options"] = {"changes": [{"value": {field: "other-boundary"}}]}
    elif location == "invalid_changes":
        arguments["changes"] = {"value": {field: "other-boundary"}}
    else:
        arguments[location][field] = "other-boundary"
    with pytest.raises(ToolPolicyViolation, match=f"run boundary: {field}"):
        policy.authorize(tool.sdk_name, arguments)


def test_business_data_does_not_bypass_request_schema_or_provider_scope() -> None:
    """名称の扱いだけを変え、制御 Schema と書込列 scope の独立検証を保持する。"""

    arguments, scope, tool, policy = _database_proposal()
    arguments["changes"][0]["action"] = "EXECUTE"
    with pytest.raises(ToolPolicyViolation, match="registered schema"):
        policy.authorize(tool.sdk_name, arguments)
    arguments["changes"][0]["action"] = "SET"
    policy.authorize(tool.sdk_name, arguments)
    scope["write_columns"].remove("example.reviews.project_id")
    with pytest.raises(ChangeProposalValidationError, match="row contract"):
        validate_database_write_proposal(
            parse_change_proposal_request(arguments), binding_scope=scope,
        )


def test_business_data_still_passes_through_sensitive_content_validation() -> None:
    """業務 value として検査を分離しても、提案 parser の秘密情報拒否は省略しない。"""

    arguments, _scope, tool, policy = _database_proposal(column="auth_token")
    policy.authorize(tool.sdk_name, arguments)
    with pytest.raises(ChangeProposalValidationError, match="sensitive content"):
        parse_change_proposal_request(arguments)


def test_other_tools_cannot_reuse_the_proposal_business_data_exception() -> None:
    """同じ形の入力でも別 capability では任意の入れ子 boundary を許可しない。"""

    arguments, _scope, tool, _policy = _database_proposal()
    other = replace(
        tool, capability="issue.read/v1", sdk_name=capability_to_sdk_name("issue.read/v1"),
    )
    policy = ToolExecutionPolicy((other,), allowed_capabilities=frozenset({"issue.read/v1"}))
    with pytest.raises(ToolPolicyViolation, match="run boundary: project_id"):
        policy.authorize(other.sdk_name, arguments)
