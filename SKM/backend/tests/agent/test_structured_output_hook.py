"""CLI の構造化結果搬送と、資源 Tool の認可を混同しないことを検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from skillmind.agent.claude import ClaudeRuntimeConfiguration, build_claude_agent_options
from tests.agent.test_claude_agent_sdk import _run_context


def options_and_hook(tmp_path: Path, schema: dict):
    """凍結 Schema と resource 監査 callback を持つ実 option factory を使う。"""
    context = replace(_run_context(tmp_path), result_schema=schema)
    authorized, denied = AsyncMock(), AsyncMock()
    options = build_claude_agent_options(
        context,
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
        on_tool_authorized=authorized,
        on_tool_denied=denied,
    )
    assert options.hooks is not None
    return options, options.hooks["PreToolUse"][0].hooks[0], authorized, denied


@pytest.mark.parametrize(
    "value,decision",
    [
        ({"answer": "fixture"}, "allow"),
        ({"answer": 1}, "deny"),
        ({"answer": "fixture", "unexpected": True}, "deny"),
        ({}, "deny"),
        (None, "deny"),
        ([], "deny"),
    ],
)
async def test_structured_output_checks_result_schema_without_resource_authorization(
    tmp_path: Path,
    value: object,
    decision: str,
) -> None:
    """固定出力 channel は schema だけを検証し、資源呼出しの許可/回执を生成しない。"""
    _, hook, authorized, denied = options_and_hook(
        tmp_path,
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    response = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "StructuredOutput",
            "tool_input": value,
        },
        "fixture-call",
        {},
    )  # type: ignore[arg-type]
    assert response["hookSpecificOutput"]["permissionDecision"] == decision
    authorized.assert_not_awaited()
    denied.assert_not_awaited()


@pytest.mark.parametrize("name", ["Bash", "Read", "StructuredOutputExtra", "structuredoutput"])
async def test_output_exception_does_not_allow_other_builtin_names(
    tmp_path: Path,
    name: str,
) -> None:
    """似た名前や他の builtin は、結果 Schema に合う object を渡しても拒否する。"""
    _, hook, authorized, _ = options_and_hook(tmp_path, {"type": "object"})
    response = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": name,
            "tool_input": {},
        },
        "fixture-call",
        {},
    )  # type: ignore[arg-type]
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    authorized.assert_not_awaited()


async def test_hook_and_sdk_schema_share_a_snapshot_not_mutable_context(tmp_path: Path) -> None:
    """元 context の入れ子変更で、実 options と hook の Schema が分岐しない。"""
    schema = {
        "type": "object",
        "properties": {"answer": {"$ref": "#/$defs/answer"}},
        "$defs": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    options, hook, _, _ = options_and_hook(tmp_path, schema)
    schema["$defs"]["answer"]["type"] = "integer"
    response = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "StructuredOutput",
            "tool_input": {"answer": "fixture"},
        },
        "fixture-call",
        {},
    )  # type: ignore[arg-type]
    assert response["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert options.output_format["schema"]["$defs"]["answer"]["type"] == "string"


async def test_result_schema_cannot_trigger_remote_schema_fetch(tmp_path: Path) -> None:
    """未解決の外部 ref は拒否し、結果搬送 hook からネットワークを開かない。"""
    _, hook, _, _ = options_and_hook(
        tmp_path,
        {
            "type": "object",
            "$ref": "https://schema.invalid/result",
        },
    )
    with patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network")):
        response = await hook(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "StructuredOutput",
                "tool_input": {},
            },
            "fixture-call",
            {},
        )  # type: ignore[arg-type]
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("failed", [False, True])
def test_output_transport_is_not_a_resource_tool_event_but_keeps_final_result(
    tmp_path: Path,
    failed: bool,
) -> None:
    """内部結果 Tool の往復は隠し、最終成功/失敗と用量を通常 event に残す。"""
    from claude_agent_sdk.types import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

    from skillmind.agent.domain import AgentEventType
    from skillmind.agent.engine import ClaudeMessageMapper
    from tests.agent.test_claude_engine import _result

    context = _run_context(tmp_path)
    mapper = ClaudeMessageMapper(context, "00000000-0000-4000-8000-000000000001")
    assert (
        mapper.map(
            AssistantMessage(
                content=[ToolUseBlock(id="output", name="StructuredOutput", input={})],
                model="fixture",
            )
        )
        == ()
    )
    assert (
        mapper.map(
            UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="output", content="fixture", is_error=failed),
                ]
            )
        )
        == ()
    )
    terminal = mapper.map(_result("00000000-0000-4000-8000-000000000001", is_error=failed))
    assert any(event.event_type is AgentEventType.USAGE_UPDATED for event in terminal)
    assert terminal[-1].event_type is (
        AgentEventType.ENGINE_FAILED if failed else AgentEventType.RESULT_COMPLETED
    )


def test_output_id_does_not_hide_a_subsequent_resource_tool(tmp_path: Path) -> None:
    """SDK が同じ ID を別 Tool に使っても、資源 Tool の表示を出力専用扱いしない。"""
    from claude_agent_sdk.types import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

    from skillmind.agent.domain import AgentEventType
    from skillmind.agent.engine import ClaudeMessageMapper

    mapper = ClaudeMessageMapper(_run_context(tmp_path), "00000000-0000-4000-8000-000000000001")
    mapper.map(
        AssistantMessage(
            content=[ToolUseBlock(id="same", name="StructuredOutput", input={})],
            model="fixture",
        )
    )
    requested = mapper.map(
        AssistantMessage(
            content=[ToolUseBlock(id="same", name="mcp__skillmind__issue_read_v1", input={})],
            model="fixture",
        )
    )
    completed = mapper.map(
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="same", content="fixture", is_error=False),
            ]
        )
    )
    assert requested[0].event_type is AgentEventType.TOOL_REQUESTED
    assert completed[0].event_type is AgentEventType.TOOL_COMPLETED
