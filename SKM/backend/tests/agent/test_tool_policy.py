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
