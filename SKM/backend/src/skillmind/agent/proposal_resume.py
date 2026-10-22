"""固定 SDK の原 transcript を只読照合し、未完 control replay と通常 resume を分ける。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import project_key_for_directory
from claude_agent_sdk.types import SessionStore

from skillmind.agent.domain import RunContext
from skillmind.effects.proposal import CHANGE_PROPOSE_SDK_NAME
from skillmind.runs.interaction import INTERACTION_REQUEST_SDK_NAME

if TYPE_CHECKING:
    from skillmind.runs.proposal_continuation import ResolvedInteraction, ResolvedProposal


async def prepare_proposal_resume(
    context: RunContext,
    store: SessionStore | None,
) -> tuple[RunContext, bool]:
    """原要求と SDK tool ID を結び、既に tool_result がある場合は通常 prompt で続ける。"""
    resolved = context.resolved_proposal
    interaction = context.resolved_interaction
    if resolved is not None:
        sdk_session_id = resolved.sdk_session_id
    elif interaction is not None:
        sdk_session_id = interaction.sdk_session_id
    else:
        return context, False
    if store is None:
        raise ValueError("Control call resume requires its original transcript store")
    async with asyncio.timeout(20):
        entries = await store.load(
            {
                "project_key": project_key_for_directory(context.workspace.cwd),
                "session_id": sdk_session_id,
            }
        )
    if not entries:
        raise ValueError("Control call resume transcript is unavailable")
    if resolved is not None:
        tool_id, pending = proposal_replay_identity(entries, resolved)
        return replace(context, resolved_proposal=replace(resolved, tool_use_id=tool_id)), pending
    assert interaction is not None
    interaction_tool_id, pending = interaction_replay_identity(entries, interaction)
    resolved_interaction = replace(interaction, tool_use_id=interaction_tool_id)
    return replace(context, resolved_interaction=resolved_interaction), pending


def _scan_deferred_calls(
    entries: Sequence[Mapping[str, Any]],
    *,
    sdk_session_id: str,
    tool_name: str,
    matches: Callable[[Mapping[str, Any]], bool],
) -> tuple[set[str], set[str], set[str]]:
    """元 assistant 呼出し・defer attachment・対応結果の tool ID を集め、entry を変更しない。"""
    calls: set[str] = set()
    parked: set[str] = set()
    completed: set[str] = set()
    for entry in entries:
        if entry.get("sessionId") != sdk_session_id:
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if (
                    entry.get("type") == "assistant"
                    and block.get("type") == "tool_use"
                    and block.get("name") == tool_name
                    and isinstance(block.get("input"), Mapping)
                    and matches(block["input"])
                    and isinstance(block.get("id"), str)
                ):
                    calls.add(block["id"])
                elif (
                    entry.get("type") == "user"
                    and block.get("type") == "tool_result"
                    and isinstance(block.get("tool_use_id"), str)
                ):
                    completed.add(block["tool_use_id"])
        attachment = entry.get("attachment")
        if (
            entry.get("type") == "attachment"
            and isinstance(attachment, Mapping)
            and attachment.get("type") == "hook_deferred_tool"
            and attachment.get("toolName") == tool_name
            and isinstance(attachment.get("toolInput"), Mapping)
            and matches(attachment["toolInput"])
            and isinstance(attachment.get("toolUseID"), str)
        ):
            parked.add(attachment["toolUseID"])
    return calls, parked, completed


def proposal_replay_identity(
    entries: Sequence[Mapping[str, Any]],
    resolved: ResolvedProposal,
) -> tuple[str, bool]:
    """元 assistant 呼出し・defer attachment・対応結果を照合し、entry を変更しない。"""
    calls, parked, completed = _scan_deferred_calls(
        entries,
        sdk_session_id=resolved.sdk_session_id,
        tool_name=CHANGE_PROPOSE_SDK_NAME,
        matches=lambda arguments: resolved.matches(arguments, resolved.sdk_session_id),
    )
    if len(calls) != 1 or parked != calls:
        raise ValueError("Original deferred proposal identity is missing or ambiguous")
    tool_id = next(iter(calls))
    return tool_id, tool_id not in completed


def interaction_replay_identity(
    entries: Sequence[Mapping[str, Any]],
    resolved: ResolvedInteraction,
) -> tuple[str | None, bool]:
    """同文の過去質問が完了済みでも、答復待ちで残る原呼出し一件だけを replay 対象にする。"""
    calls, parked, completed = _scan_deferred_calls(
        entries,
        sdk_session_id=resolved.sdk_session_id,
        tool_name=INTERACTION_REQUEST_SDK_NAME,
        matches=lambda arguments: resolved.matches(arguments, resolved.sdk_session_id),
    )
    open_calls = (calls & parked) - completed
    if len(open_calls) == 1:
        return next(iter(open_calls)), True
    if not open_calls and calls & parked:
        return None, False
    raise ValueError("Original deferred interaction identity is missing or ambiguous")
