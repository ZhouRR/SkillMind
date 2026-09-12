"""SDK の API 応答境界を保ち、再生成本文を既存 JSON 復元へ渡す。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from claude_agent_sdk._internal.message_parser import parse_message
from skillmind.agent import interpreter_completion
from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.skills.interpreter import load_interpreter_system_skill
from skillmind.skills.interpreter_execution import InterpreterErrorCode, InterpreterExecutionError
from skillmind.skills.model_interpreter import ModelSkillInterpreter


def _assistant(
    *texts: str,
    message_id: object = "response-one",
    thinking: bool = False,
    stop: str | None = None,
) -> dict[str, Any]:
    """API ID と block ごとに異なる SDK UUID を持つ合成 wire を返す。"""

    content: list[dict[str, str]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "fixture", "signature": "fixture"})
    content.extend({"type": "text", "text": text} for text in texts)
    message = {"model": "fixture-model", "content": content, "stop_reason": stop}
    if message_id is not None:
        message["id"] = message_id
    return {
        "type": "assistant", "session_id": "fixture-session", "uuid": str(uuid4()),
        "message": message,
    }


def _result(**changes: Any) -> dict[str, Any]:
    """本文と structured output を持たない正常終態を基本に、必要な失敗だけを注入する。"""

    return {
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 1, "duration_api_ms": 1, "num_turns": 1,
        "session_id": "fixture-session", "stop_reason": "end_turn", **changes,
    }


async def _interpret(
    monkeypatch: pytest.MonkeyPatch, messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """query を合成 wire に置換し、実 SDK parser と既存 Interpreter decode を通す。"""

    calls = []

    async def query(**kwargs: Any):
        """CLI やモデルを呼ばず、一回の SDK query 内の応答だけを返す。"""

        calls.append(True)
        assert kwargs["options"].max_turns == 1
        for message in messages:
            yield parse_message(message)

    monkeypatch.setattr(interpreter_completion, "query", query)
    root = Path(__file__).resolve().parents[3] / "skills" / "skillmind-skill-interpreter"
    schema = {"type": "object"}
    identity = load_interpreter_system_skill(root, generation_schema=schema)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    interpreter = ModelSkillInterpreter(
        completion_client=client, system_skill_root=root, response_schema=schema,
        accept_prompt_json=True,
    )
    try:
        return await interpreter.interpret(
            {"interpreter": identity.to_dict()}, model="fixture-model", parameters={},
        )
    finally:
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_replacement_api_response_discards_truncated_prefix(monkeypatch):
    """先行応答の打切り JSON に、新 API 応答の完全な候補を連結しない。"""

    result = await _interpret(monkeypatch, [
        _assistant('{"old":', stop="max_tokens"),
        _assistant(thinking=True, message_id="response-two"),
        _assistant('{"ok":true}', message_id="response-two", stop="end_turn"),
        _result(),
    ])
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_cross_response_continuation_is_not_repaired_by_concatenation(monkeypatch):
    """別 API の後半だけを前半へ補って候補を捏造せず、既存 JSON 検査で拒否する。"""

    with pytest.raises(InterpreterExecutionError) as failure:
        await _interpret(monkeypatch, [
            _assistant('{"ok":', stop="max_tokens"),
            _assistant('true}', message_id="response-two", stop="end_turn"),
            _result(),
        ])
    assert failure.value.code is InterpreterErrorCode.INVALID_JSON


@pytest.mark.asyncio
async def test_same_api_response_preserves_multiple_text_blocks(monkeypatch):
    """同じ API ID の複数 SDK message と一 message 内の TextBlock の順序を保つ。"""

    result = await _interpret(monkeypatch, [
        _assistant(thinking=True),
        _assistant('{"ok":'),
        _assistant('true,', '"items":[1,2]}', stop="end_turn"),
        _result(),
    ])
    assert result == {"ok": True, "items": [1, 2]}


@pytest.mark.asyncio
async def test_new_thinking_only_response_cannot_reuse_old_valid_json(monkeypatch):
    """新応答に text がなくても最初の thinking block で旧候補を破棄する。"""

    with pytest.raises(InterpreterExecutionError) as failure:
        await _interpret(monkeypatch, [
            _assistant('{"old":true}'),
            _assistant(thinking=True, message_id="response-two", stop="end_turn"),
            _result(),
        ])
    assert failure.value.code is InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, "", " \t", 7, False, {"id": "unexpected"}])
async def test_missing_or_invalid_ids_never_join_separate_messages(monkeypatch, message_id):
    """ID が未提供・空・異型でも、各 message を独立させて部分 JSON を連結しない。"""

    with pytest.raises(InterpreterExecutionError) as failure:
        await _interpret(monkeypatch, [
            _assistant('{"ok":', message_id=message_id),
            _assistant('true}', message_id=message_id),
            _result(),
        ])
    assert failure.value.code is InterpreterErrorCode.INVALID_JSON


@pytest.mark.asyncio
async def test_unidentified_message_retains_its_own_complete_text_blocks(monkeypatch):
    """ID 不明時も同じ message 内の block は保存し、直前の ID 付き本文は混ぜない。"""

    result = await _interpret(monkeypatch, [
        _assistant('{"old":'),
        _assistant('{"ok":', 'true}', message_id=None, stop="end_turn"),
        _result(),
    ])
    assert result == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,expected", [
    (_result(is_error=True, api_error_status=429), InterpreterErrorCode.PROVIDER_ERROR),
    (_result(stop_reason="max_tokens"), InterpreterErrorCode.TRUNCATED_OUTPUT),
    (_result(subtype="error_max_turns", is_error=True), InterpreterErrorCode.TRUNCATED_OUTPUT),
    (
        _result(subtype="error_max_structured_output_retries", is_error=True),
        InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE,
    ),
])
async def test_valid_last_json_does_not_override_terminal_failure(monkeypatch, terminal, expected):
    """最後の本文が JSON として完全でも、既存 Provider/打切り/構造化失敗を優先する。"""

    with pytest.raises(InterpreterExecutionError) as failure:
        await _interpret(monkeypatch, [
            _assistant('{"old":', stop="max_tokens"),
            _assistant('{"ok":true}', message_id="response-two"),
            terminal,
        ])
    assert failure.value.code is expected
