"""Skill 解釈の model transport も、主 Run と同じ同梱 CLI を使うことを確認する。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from claude_agent_sdk import (
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
)
from claude_agent_sdk._internal.message_parser import parse_message

from skillmind.agent import interpreter_completion
from skillmind.agent.claude import _AGENT_ENVIRONMENT_KEYS, ClaudeRuntimeConfiguration
from skillmind.agent.claude_build import bundled_claude_build
from skillmind.core.logging import JsonLogFormatter
from skillmind.skills.interpreter import load_interpreter_system_skill
from skillmind.skills.interpreter_execution import InterpreterErrorCode, InterpreterExecutionError
from skillmind.skills.model_interpreter import (
    ModelProviderError,
    ModelSkillInterpreter,
    ModelStructuredOutputError,
)
from tests.agent.test_claude_engine import _result

PRIVATE = "fixture-private-password-and-model-body"


@pytest.mark.asyncio
async def test_interpreter_explicitly_selects_verified_bundle(monkeypatch):
    """query は fake とし、構造化出力と実 options の CLI path を検証する。"""
    seen = []

    async def query(*, prompt, options):
        """model/ネットワークを起動せず、呼出しに渡された options を採取する。"""
        seen.append(options)
        yield _result(str(uuid4()))

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    result = await client.complete(
        system_prompt="Fixture",
        user_message="Fixture input",
        response_schema={"type": "object"},
        model="claude-test",
        parameters={},
    )
    assert result.structured_output == {"summary": "ok"}
    assert len(seen) == 1 and seen[0].cli_path == bundled_claude_build().cli_path


@pytest.mark.asyncio
@pytest.mark.parametrize("use_fallback", [False, True])
async def test_interpreter_passes_output_token_limit_through_isolated_environment(
    monkeypatch, use_fallback,
):
    """環境または fallback の 64000 を実 options へ渡し、他の設定や資格情報を消す。"""

    key = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
    for allowed_key in _AGENT_ENVIRONMENT_KEYS:
        monkeypatch.delenv(allowed_key, raising=False)
    if not use_fallback:
        monkeypatch.setenv(key, "64000")
    monkeypatch.setenv("SKILLMIND_DATABASE_URL", PRIVATE)
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "99")
    configuration = ClaudeRuntimeConfiguration.from_environ(
        fallback={key: "64000"} if use_fallback else None
    )
    seen = []

    async def query(*, prompt, options):
        """実モデルを起動せず、SDK へ渡す最終環境を採取する。"""
        seen.append(options)
        yield _result(str(uuid4()))

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(configuration)
    result = await client.complete(
        system_prompt="Fixture", user_message="Fixture input", response_schema={"type": "object"},
        model="fixture-model", parameters={},
    )
    assert result.structured_output == {"summary": "ok"}
    assert len(seen) == 1
    assert seen[0].env[key] == "64000"
    assert seen[0].env["SKILLMIND_DATABASE_URL"] == ""
    assert seen[0].env["CLAUDE_CODE_MAX_RETRIES"] == ""
    assert PRIVATE not in str(seen[0].env)


@pytest.mark.asyncio
async def test_interpreter_refuses_unavailable_bundle_before_query(monkeypatch):
    """CLI がないときに通常の SDK 探索へ戻って課金を始めない。"""

    def missing():
        """install 破損を、実 package を変更せず注入する。"""
        raise RuntimeError("Pinned runtime unavailable")

    def query(**kwargs):
        """拒否後に query を一度でも作成すると失敗する。"""
        raise AssertionError("Unexpected model query")

    monkeypatch.setattr(interpreter_completion, "bundled_claude_build", missing)
    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    with pytest.raises(RuntimeError, match="Pinned runtime"):
        await client.complete(
            system_prompt="Fixture",
            user_message="Fixture input",
            response_schema={"type": "object"},
            model="claude-test",
            parameters={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error,kind,exit_code", [
    (ProcessError(PRIVATE, exit_code=17, stderr=PRIVATE), "ProcessError", 17),
    (CLIConnectionError(PRIVATE), "CLIConnectionError", None),
    (CLINotFoundError(PRIVATE, cli_path=PRIVATE), "CLINotFoundError", None),
    (CLIJSONDecodeError(PRIVATE, ValueError(PRIVATE)), "CLIJSONDecodeError", None),
    (ClaudeSDKError(PRIVATE), "ClaudeSDKError", None),
    (type(PRIVATE, (ClaudeSDKError,), {})(PRIVATE), "OtherClaudeSDKError", None),
])
async def test_sdk_failure_logs_static_kind_without_private_exception_fields(
    monkeypatch, caplog, error, kind, exit_code,
):
    """fake query の例外本文/未知 class 名を捨て、実 JSON logger の静的分類だけを確認する。"""

    async def query(**kwargs):
        """実 CLI/model を起動せず、固定した SDK 障害を一度だけ返す。"""
        raise error
        yield  # pragma: no cover - SDK の async generator 形状だけを保持する。

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    with pytest.raises(ModelProviderError):
        await client.complete(
            system_prompt=PRIVATE, user_message=PRIVATE, response_schema={"type": "object"},
            model="claude-test", parameters={},
        )
    records = [
        record for record in caplog.records
        if getattr(record, "skillmind_event", None) == "skill.interpret.completion_diagnostic"
    ]
    assert len(records) == 1
    serialized = JsonLogFormatter().format(records[0])
    payload = json.loads(serialized)
    assert payload["error_code"] == "provider_error"
    assert payload["provider_error_kind"] == kind
    assert payload.get("provider_exit_code") == exit_code
    assert ("provider_exit_code" in payload) is (exit_code is not None)
    assert PRIVATE not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [None, True, PRIVATE])
async def test_process_diagnostic_does_not_log_noninteger_exit_code(monkeypatch, caplog, exit_code):
    """型の壊れた SDK 診断値や bool を、ログの整数 exit code として採用しない。"""

    error = ProcessError(PRIVATE)
    error.exit_code = exit_code

    async def query(**kwargs):
        """外部呼出しなしで、型の壊れた終了情報を返す。"""
        raise error
        yield  # pragma: no cover - SDK の async generator 形状だけを保持する。

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    with pytest.raises(ModelProviderError):
        await client.complete(
            system_prompt=PRIVATE, user_message=PRIVATE, response_schema={"type": "object"},
            model="claude-test", parameters={},
        )
    records = [
        record for record in caplog.records
        if getattr(record, "skillmind_event", None) == "skill.interpret.completion_diagnostic"
    ]
    assert len(records) == 1
    serialized = JsonLogFormatter().format(records[0])
    assert "provider_exit_code" not in json.loads(serialized) and PRIVATE not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype,code", [
    ("success", "provider_error"),
    ("error_during_execution", "provider_error"),
    ("error_max_budget_usd", "provider_error"),
    ("error_max_structured_output_retries", "structured_output_unavailable"),
    ("error_max_turns", "truncated_output"),
    (PRIVATE, "provider_error"),
])
async def test_result_diagnostic_preserves_classification_without_logging_model_body(
    monkeypatch, caplog, subtype, code,
):
    """既知 subtype と失敗分類だけを記録し、raw result/errors/未知 subtype を捨てる。"""

    calls = []

    async def query(**kwargs):
        """一回の合成 Result を返し、診断追加で query の再試行が増えないことも確認する。"""
        calls.append(True)
        yield replace(_result(str(uuid4()), is_error=True), subtype=subtype,
                      result=PRIVATE, errors=[PRIVATE], structured_output={"private": PRIVATE})

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    arguments = dict(system_prompt=PRIVATE, user_message=PRIVATE,
                     response_schema={"type": "object"}, model="claude-test", parameters={})
    if code == "truncated_output":
        assert (await client.complete(**arguments)).truncated
    else:
        expected = (
            ModelStructuredOutputError
            if code == "structured_output_unavailable" else ModelProviderError
        )
        with pytest.raises(expected):
            await client.complete(**arguments)
    records = [
        record for record in caplog.records
        if getattr(record, "skillmind_event", None) == "skill.interpret.completion_diagnostic"
    ]
    assert len(records) == len(calls) == 1
    serialized = JsonLogFormatter().format(records[0])
    payload = json.loads(serialized)
    assert payload["error_code"] == code
    assert payload["provider_result_subtype"] == ("unrecognized" if subtype == PRIVATE else subtype)
    assert PRIVATE not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("api_error_status,expected", [
    (100, 100), (429, 429), (500, 500), (529, 529), (599, 599),
    (None, None), (True, None), (False, None), ("429", None), (PRIVATE, None),
    (429.0, None), (99, None), (600, None), (-1, None), ({"private": PRIVATE}, None),
])
async def test_api_error_result_logs_only_bounded_integer_status_without_model_body(
    monkeypatch, caplog, api_error_status, expected,
):
    """success subtype の API 障害も失敗のままとし、本文を含まない HTTP 状態だけを残す。"""

    calls = []

    async def query(**kwargs):
        """実モデルを起動せず、SDK が提供する API 障害 Result を一回だけ返す。"""
        calls.append(True)
        yield replace(
            _result(str(uuid4()), is_error=True), subtype="success",
            api_error_status=api_error_status, result=PRIVATE, errors=[PRIVATE],
            structured_output={"private": PRIVATE},
        )

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    with pytest.raises(ModelProviderError):
        await client.complete(
            system_prompt=PRIVATE, user_message=PRIVATE, response_schema={"type": "object"},
            model="claude-test", parameters={},
        )
    records = [
        record for record in caplog.records
        if getattr(record, "skillmind_event", None) == "skill.interpret.completion_diagnostic"
    ]
    assert len(records) == len(calls) == 1
    serialized = JsonLogFormatter().format(records[0])
    payload = json.loads(serialized)
    assert payload["error_code"] == "provider_error"
    assert payload["provider_result_subtype"] == "success"
    assert payload.get("provider_api_error_status") == expected
    assert ("provider_api_error_status" in payload) is (expected is not None)
    assert PRIVATE not in serialized


def _output_limit_assistant_wire(session_id, *, error="max_output_tokens", stop="stop_sequence"):
    """CLI が出す上限エラーを再現し、本文は合成値だけにする。"""

    return {
        "type": "assistant", "session_id": session_id, "error": error,
        "message": {"model": "fixture-model", "stop_reason": stop,
                    "content": [{"type": "text", "text": PRIVATE}]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [None, {"private": PRIVATE}])
async def test_actual_output_limit_wire_rejects_thinking_only_and_partial_candidate(
    monkeypatch, caplog, structured,
):
    """4 回の thinking 打切りと最終 SDK error を解析し、候補復元前の失敗を確認する。"""

    session_id = str(uuid4())
    calls = []

    async def query(**kwargs):
        """実 SDK parser を通すが、CLI/model は呼ばず一回分の wire だけを返す。"""
        calls.append(True)
        for _ in range(4):
            yield parse_message({
                "type": "assistant", "session_id": session_id,
                "message": {
                    "model": "fixture-model", "stop_reason": "max_tokens",
                    "usage": {"output_tokens": 32000},
                    "content": [{"type": "thinking", "thinking": PRIVATE, "signature": PRIVATE}],
                },
            })
        yield parse_message(_output_limit_assistant_wire(session_id))
        yield parse_message({
            "type": "result", "subtype": "success", "is_error": True,
            "session_id": session_id, "duration_ms": 100, "duration_api_ms": 80,
            "num_turns": 1, "stop_reason": "stop_sequence", "api_error_status": None,
            "result": PRIVATE, "errors": [PRIVATE], "structured_output": structured,
        })

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    system_skill = Path(__file__).resolve().parents[3] / "skills" / "skillmind-skill-interpreter"
    schema = {"type": "object"}
    identity = load_interpreter_system_skill(system_skill, generation_schema=schema)
    interpreter = ModelSkillInterpreter(
        completion_client=client, system_skill_root=system_skill, response_schema=schema,
        accept_prompt_json=True,
    )
    with pytest.raises(InterpreterExecutionError) as failure:
        await interpreter.interpret(
            {"interpreter": identity.to_dict()}, model="fixture-model", parameters={},
        )
    assert failure.value.code is InterpreterErrorCode.TRUNCATED_OUTPUT
    records = [
        record for record in caplog.records
        if getattr(record, "skillmind_event", None) == "skill.interpret.completion_diagnostic"
    ]
    assert len(records) == len(calls) == 1
    serialized = JsonLogFormatter().format(records[0])
    assert json.loads(serialized)["error_code"] == "truncated_output"
    assert PRIVATE not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype,is_error,stop,status,last_error,expected", [
    ("success", True, "max_tokens", None, None, "truncated_output"),
    ("success", False, "max_tokens", None, None, "truncated_output"),
    ("success", True, "max_tokens", 429, "max_output_tokens", "provider_error"),
    ("success", True, None, 500, "max_output_tokens", "provider_error"),
    ("success", True, None, "429", "max_output_tokens", "provider_error"),
    ("success", True, None, 0, "max_output_tokens", "provider_error"),
    ("error_during_execution", True, "max_tokens", None, "max_output_tokens", "provider_error"),
    ("error_max_structured_output_retries", True, "max_tokens", None,
     "max_output_tokens", "structured_output_unavailable"),
    ("success", True, None, None, "authentication_failed", "provider_error"),
    ("success", True, None, None, PRIVATE, "provider_error"),
    ("error_max_turns", True, None, None, "max_output_tokens", "truncated_output"),
    ("error_max_turns", True, None, 429, "max_output_tokens", "provider_error"),
    ("success", False, "end_turn", None, None, None),
])
async def test_output_limit_signal_does_not_mask_terminal_provider_or_structured_failure(
    monkeypatch, caplog, subtype, is_error, stop, status, last_error, expected,
):
    """最新 assistant と終態だけで分類し、古い打切りや HTTP 障害を成功へ変えない。"""

    session_id = str(uuid4())
    calls = []

    async def query(**kwargs):
        """前の打切りから SDK が進んだ場合も合成し、再呼出しを検出する。"""
        calls.append(True)
        yield parse_message(_output_limit_assistant_wire(session_id))
        yield parse_message(_output_limit_assistant_wire(session_id, error=last_error))
        yield replace(
            _result(session_id, is_error=is_error), subtype=subtype, stop_reason=stop,
            api_error_status=status, structured_output={"ok": True}, result=PRIVATE,
        )

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    arguments = dict(system_prompt=PRIVATE, user_message=PRIVATE,
                     response_schema={"type": "object"}, model="fixture-model", parameters={})
    if expected in {None, "truncated_output"}:
        result = await client.complete(**arguments)
        assert result.truncated is (expected == "truncated_output")
    else:
        exception = (
            ModelStructuredOutputError
            if expected == "structured_output_unavailable" else ModelProviderError
        )
        with pytest.raises(exception):
            await client.complete(**arguments)
    assert len(calls) == 1
    records = [
        record for record in caplog.records
        if getattr(record, "skillmind_event", None) == "skill.interpret.completion_diagnostic"
    ]
    assert len(records) == (0 if expected is None else 1)
    if records:
        serialized = JsonLogFormatter().format(records[0])
        assert json.loads(serialized)["error_code"] == expected
        assert PRIVATE not in serialized
