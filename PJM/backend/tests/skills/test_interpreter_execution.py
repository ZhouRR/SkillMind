"""SDK 非依存な Interpreter 実行契約、idempotency key、失敗分類を検証する。"""

from __future__ import annotations

import pytest

from projectmind.skills.interpreter_execution import (
    FixtureSkillInterpreter,
    InterpreterErrorCode,
    InterpreterExecutionError,
    compute_execution_key,
)

_REQUEST = {
    "request_version": "projectmind.skill-interpreter.request/v1",
    "source": {"content_hash": "sha256:" + ("a" * 64)},
    "static_analysis": {"checksum": "sha256:" + ("b" * 64)},
    "capability_catalog": {"checksum": "sha256:" + ("c" * 64)},
    "interpreter": {"skill_key": "projectmind-skill-interpreter", "version": "1.0.0"},
    "previous_interpretation": None,
    "adjustment": None,
}


def test_execution_key_is_deterministic_across_dict_ordering() -> None:
    """同一 identity は key 生成順序に依存せず同じ execution key を返す。"""

    reordered = {key: _REQUEST[key] for key in reversed(list(_REQUEST))}
    first = compute_execution_key(_REQUEST, model="claude-opus-4-8", parameters={"a": 1, "b": 2})
    second = compute_execution_key(reordered, model="claude-opus-4-8", parameters={"b": 2, "a": 1})

    assert first == second
    assert first.startswith("sha256:")


def test_execution_key_changes_with_model_parameters_or_request() -> None:
    """model、parameter、request の差異はいずれも新しい identity を生む。"""

    base = compute_execution_key(_REQUEST, model="claude-opus-4-8", parameters={})
    other_model = compute_execution_key(_REQUEST, model="claude-sonnet-5", parameters={})
    other_params = compute_execution_key(_REQUEST, model="claude-opus-4-8", parameters={"t": 1})
    other_request = compute_execution_key(
        {**_REQUEST, "adjustment": {"instruction": "narrow"}},
        model="claude-opus-4-8",
        parameters={},
    )

    assert len({base, other_model, other_params, other_request}) == 4


def test_execution_key_isolated_by_organization_scope() -> None:
    """同一 content の cross-Organization job/channel identity を共有させない。"""

    first = compute_execution_key(
        _REQUEST,
        model="claude-opus-4-8",
        parameters={},
        scope_id="00000000-0000-4000-8000-000000000001",
    )
    second = compute_execution_key(
        _REQUEST,
        model="claude-opus-4-8",
        parameters={},
        scope_id="00000000-0000-4000-8000-000000000002",
    )

    assert first != second


@pytest.mark.asyncio
async def test_fixture_interpreter_returns_isolated_copy() -> None:
    """Fixture interpreter は request/model を無視し、可変でない複製を返す。"""

    response = {"report": {"summary": "ok"}, "nested": {"value": 1}}
    interpreter = FixtureSkillInterpreter(response)

    first = await interpreter.interpret({"any": "request"}, model="m", parameters={})
    first["nested"]["value"] = 999
    second = await interpreter.interpret({"other": "request"}, model="m2", parameters={"x": 1})

    assert second == response
    assert second["nested"]["value"] == 1


def test_execution_error_carries_stable_code() -> None:
    """失敗 error は分類 code を保持し、default message に来源値を含めない。"""

    error = InterpreterExecutionError(InterpreterErrorCode.TIMEOUT)

    assert error.code is InterpreterErrorCode.TIMEOUT
    assert str(error) == "timeout"
