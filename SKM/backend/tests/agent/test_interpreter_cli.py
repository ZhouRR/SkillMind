"""Skill 解釈の model transport も、主 Run と同じ同梱 CLI を使うことを確認する。"""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from claude_agent_sdk import (
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
)
from skillmind.agent import interpreter_completion
from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.claude_build import bundled_claude_build
from skillmind.core.logging import JsonLogFormatter
from skillmind.skills.model_interpreter import ModelProviderError, ModelStructuredOutputError
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
