"""回答済み interaction の原 deferred 呼出しが、再質問ではなく答復として完了することを検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from skillmind.agent.claude import ClaudeRuntimeConfiguration, build_claude_agent_options
from skillmind.agent.control_providers import DeferredInteractionProvider
from skillmind.agent.domain import RegisteredTool
from skillmind.agent.proposal_resume import interaction_replay_identity
from skillmind.agent.tool_gateway import RunToolContext
from skillmind.runs.interaction import (
    INTERACTION_REQUEST_CAPABILITY,
    INTERACTION_REQUEST_SCHEMA,
    INTERACTION_REQUEST_SDK_NAME,
)
from skillmind.runs.proposal_continuation import ResolvedInteraction
from tests.agent.test_claude_agent_sdk import _run_context

SESSION = "session-1"


def _request() -> dict[str, Any]:
    """公開 contract を満たす最小の質問入力。"""
    return {
        "interaction_type": "CLARIFICATION",
        "prompt": "Which environment?",
        "rationale": "The target is ambiguous.",
        "impact": "Changes which data is read.",
        "options": [],
        "allow_multiple": False,
        "required": True,
        "expires_in_seconds": 3600,
        "continuation_mode": "RESUME",
        "checkpoint": {
            "summary": "Waiting for the environment.",
            "confirmed_facts": [],
            "evidence_refs": [],
            "artifact_refs": [],
            "change_proposal_refs": [],
        },
    }


def _resolved(arguments: dict[str, Any], tool_use_id: str | None = None) -> ResolvedInteraction:
    """原要求から保存される prompt/options と答復だけを持つ descriptor。"""
    return ResolvedInteraction(
        interaction_id="3f0c4a66-7a53-4b0c-9a0e-0d6a1f1a0b11",
        sdk_session_id=SESSION,
        interaction_type=arguments["interaction_type"],
        prompt={
            "prompt": arguments["prompt"],
            "rationale": arguments["rationale"],
            "impact": arguments["impact"],
            "allow_multiple": arguments["allow_multiple"],
        },
        options=tuple(arguments["options"]),
        response={"text": "production"},
        tool_use_id=tool_use_id,
    )


def _entries(arguments: dict[str, Any], *call_ids: str) -> list[dict[str, Any]]:
    """各 call の assistant tool_use と defer attachment を並べた transcript。"""
    entries: list[dict[str, Any]] = []
    for call_id in call_ids:
        entries.append({
            "sessionId": SESSION, "type": "assistant",
            "message": {"content": [{
                "type": "tool_use", "id": call_id,
                "name": INTERACTION_REQUEST_SDK_NAME, "input": arguments,
            }]},
        })
        entries.append({
            "sessionId": SESSION, "type": "attachment",
            "attachment": {
                "type": "hook_deferred_tool", "toolUseID": call_id,
                "toolName": INTERACTION_REQUEST_SDK_NAME, "toolInput": arguments,
            },
        })
    return entries


def _result(call_id: str) -> dict[str, Any]:
    """指定 call の tool_result entry。"""
    return {
        "sessionId": SESSION, "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]},
    }


def test_matches_requires_same_session_and_public_question() -> None:
    """別 Session や別内容の質問は、答復済みの原呼出しとして扱わない。"""
    arguments = _request()
    resolved = _resolved(arguments)
    assert resolved.matches(arguments, SESSION)
    assert not resolved.matches(arguments, "other")
    changed = deepcopy(arguments)
    changed["prompt"] = "Another question?"
    assert not resolved.matches(changed, SESSION)
    assert not resolved.matches({"prompt": "invalid"}, SESSION)


def test_replay_targets_the_single_unanswered_call() -> None:
    """完了済みの同文質問があっても、未完の原呼出し一件だけを replay 対象にする。"""
    arguments = _request()
    resolved = _resolved(arguments)
    entries = [*_entries(arguments, "old"), _result("old"), *_entries(arguments, "new")]
    assert interaction_replay_identity(entries, resolved) == ("new", True)
    entries.append(_result("new"))
    assert interaction_replay_identity(entries, resolved) == (None, False)


@pytest.mark.parametrize("damage", ["missing", "other_session", "ambiguous"])
def test_replay_identity_rejects_missing_or_ambiguous_calls(damage: str) -> None:
    """原呼出しを特定できない transcript から、推測で答復を返さない。"""
    arguments = _request()
    resolved = _resolved(arguments)
    entries = _entries(arguments, "one")
    if damage == "missing":
        entries = []
    elif damage == "other_session":
        for entry in entries:
            entry["sessionId"] = "other"
    else:
        entries += _entries(arguments, "two")
    with pytest.raises(ValueError, match="missing or ambiguous"):
        interaction_replay_identity(entries, resolved)


def _tool() -> RegisteredTool:
    """Run に登録された platform の interaction Tool。"""
    return RegisteredTool(
        capability=INTERACTION_REQUEST_CAPABILITY, sdk_name=INTERACTION_REQUEST_SDK_NAME,
        provider="platform", integration_id=None, input_schema=INTERACTION_REQUEST_SCHEMA,
    )


async def _decision(tmp_path: Path, resolved: ResolvedInteraction | None,
                    arguments: dict[str, Any], call_id: str) -> str:
    """PreToolUse hook が同じ質問呼出しへ返す許可判断を取り出す。"""
    context = replace(
        _run_context(tmp_path), tools=(_tool(),), resolved_interaction=resolved,
        permission_snapshot={
            "mode": "auto_read_only", "allowed_capabilities": [INTERACTION_REQUEST_CAPABILITY],
        },
    )
    options = build_claude_agent_options(
        context,
        mcp_server={"type": "sdk", "name": "skillmind", "instance": object()},
        configuration=ClaudeRuntimeConfiguration(environment={}),
        deferred_tool_names=frozenset({INTERACTION_REQUEST_SDK_NAME}),
    )
    assert options.hooks is not None
    hook = options.hooks["PreToolUse"][0].hooks[0]
    data = {
        "hook_event_name": "PreToolUse", "session_id": SESSION,
        "transcript_path": "/tmp/transcript", "cwd": str(options.cwd),
        "tool_name": INTERACTION_REQUEST_SDK_NAME, "tool_input": arguments,
        "tool_use_id": call_id,
    }
    output = await hook(data, call_id, {"signal": None})  # type: ignore[arg-type]
    return str(output["hookSpecificOutput"]["permissionDecision"])


@pytest.mark.asyncio
async def test_hook_defers_new_question_but_allows_the_answered_original(tmp_path: Path) -> None:
    """答復済みの同一 tool_use だけを通し、他の質問は従来通り durable に待機させる。"""
    arguments = _request()
    assert await _decision(tmp_path, None, arguments, "call-1") == "defer"
    resolved = _resolved(arguments, tool_use_id="call-1")
    assert await _decision(tmp_path, resolved, arguments, "call-1") == "allow"
    assert await _decision(tmp_path, resolved, arguments, "call-2") == "defer"
    changed = deepcopy(arguments)
    changed["prompt"] = "Another question?"
    assert await _decision(tmp_path, resolved, changed, "call-1") == "defer"


@pytest.mark.asyncio
async def test_provider_returns_audited_answer_only_for_resolved_call(tmp_path: Path) -> None:
    """Provider は原質問へ保存済み答復と現 Brief を返し、未解決の呼出しは拒否する。"""
    from skillmind.agent.tool_gateway import ToolProviderError

    arguments = _request()
    run = replace(_run_context(tmp_path), resolved_interaction=_resolved(arguments, "call-1"))
    context = RunToolContext(
        run_id=run.run_id, run_attempt_id=run.run_attempt_id, project_id=run.project_id,
        user_id=run.user_id, tool=_tool(), workspace=run.workspace, run=run,
    )
    result = await DeferredInteractionProvider().execute(context, arguments)
    assert result.response["deferred"] is False
    assert result.response["response"] == {"text": "production"}
    assert result.response["continuation_prompt"] == run.prompt
    assert result.evidence
    changed = deepcopy(arguments)
    changed["prompt"] = "Another question?"
    with pytest.raises(ToolProviderError):
        await DeferredInteractionProvider().execute(context, changed)
    bare = replace(context, run=_run_context(tmp_path))
    with pytest.raises(ToolProviderError):
        await DeferredInteractionProvider().execute(bare, arguments)
