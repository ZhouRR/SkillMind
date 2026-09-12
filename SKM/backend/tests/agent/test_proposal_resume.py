"""原 SDK call の識別と、処理済み transcript の再開境界を検証する。"""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from claude_agent_sdk.types import DeferredToolUse

from skillmind.agent.domain import AgentEventType
from skillmind.agent.engine import ClaudeMessageMapper
from skillmind.agent.proposal_resume import proposal_replay_identity
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.proposal import CHANGE_PROPOSE_SDK_NAME
from skillmind.runs.proposal_continuation import ResolvedProposal
from tests.agent.test_claude_engine import _result, _run_context


def _transcript():
    """原 tool_use/defer entry と完全要求 fingerprint を作る。"""
    arguments = {"idempotency_key": "original", "changes": [{"value": 1}]}
    resolved = ResolvedProposal(
        sha256_hex(canonical_json(arguments)), "session", "cp_test", "APPLIED"
    )
    entries = [
        {
            "sessionId": "session",
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "original",
                        "name": CHANGE_PROPOSE_SDK_NAME,
                        "input": arguments,
                    }
                ]
            },
        },
        {
            "sessionId": "session",
            "type": "attachment",
            "attachment": {
                "type": "hook_deferred_tool",
                "toolUseID": "original",
                "toolName": CHANGE_PROPOSE_SDK_NAME,
                "toolInput": arguments,
            },
        },
    ]
    return entries, resolved


def test_original_tool_result_changes_resume_mode_without_changing_transcript():
    """原 Tool が完了済みなら通常 prompt に戻し、別 tool_result は完了証明にしない。"""
    entries, resolved = _transcript()
    original = deepcopy(entries)
    assert proposal_replay_identity(entries, resolved) == ("original", True)
    assert entries == original
    entries.append(
        {
            "sessionId": "session",
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "another"}]},
        }
    )
    assert proposal_replay_identity(entries, resolved) == ("original", True)
    entries[-1]["message"]["content"][0]["tool_use_id"] = "original"
    assert proposal_replay_identity(entries, resolved) == ("original", False)


@pytest.mark.parametrize(
    "damage", ["missing_call", "missing_defer", "session", "input", "tool_id", "ambiguous"]
)
def test_transcript_mismatch_cannot_resolve_control_request(damage):
    """別 ID/Session/要求や不完全な履歴を曖昧に補完しない。"""
    entries, resolved = _transcript()
    if damage == "missing_call":
        entries.pop(0)
    elif damage == "missing_defer":
        entries.pop()
    elif damage == "session":
        entries[0]["sessionId"] = "another"
    elif damage == "input":
        entries[0]["message"]["content"][0]["input"] = {"idempotency_key": "changed"}
    elif damage == "tool_id":
        entries[1]["attachment"]["toolUseID"] = "another"
    else:
        other = deepcopy(entries[0])
        other["message"]["content"][0]["id"] = "another"
        entries.append(other)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        proposal_replay_identity(entries, resolved)


def test_unavailable_deferred_tool_is_engine_failure_not_new_proposal(tmp_path):
    """CLI が復元不能な元 call を返しても Worker の新規提案保存へ流さない。"""
    sdk_id = str(uuid4())
    message = _result(sdk_id)
    message.stop_reason = "tool_deferred_unavailable"
    message.deferred_tool_use = DeferredToolUse(
        "original", CHANGE_PROPOSE_SDK_NAME, {"private": "input"}
    )
    events = ClaudeMessageMapper(_run_context(tmp_path), sdk_id).map(message)
    assert events[-1].event_type is AgentEventType.ENGINE_FAILED
    assert events[-1].payload["reason"] == "deferred_tool_unavailable"
    assert "change_proposal_request" not in events[-1].payload
    assert "private" not in str(events)
