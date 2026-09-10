"""登録済み Tool の名前、Schema、Run boundary による hard deny を検証する。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from skillmind.agent.domain import RegisteredTool
from skillmind.agent.tool_policy import (
    ToolExecutionPolicy,
    ToolPolicyViolation,
    capability_to_sdk_name,
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
