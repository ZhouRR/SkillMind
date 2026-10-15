"""Schema 診断が修正箇所を示し、業務値や動的 key を返さないことを検証する。"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.tool_diagnostics import schema_error_summary


@pytest.mark.parametrize("rule,value,expected", [
    ({"maximum": 50}, 100, "must be <= 50 (maximum)"),
    ({"minimum": 1}, 0, "must be >= 1 (minimum)"),
    ({"exclusiveMaximum": 5}, 5, "must be < 5 (exclusiveMaximum)"),
    ({"exclusiveMinimum": 5}, 5, "must be > 5 (exclusiveMinimum)"),
    ({"type": "integer"}, "private-value", "expected integer"),
    ({"maxLength": 3}, "private-value", "at most 3 characters"),
    ({"minLength": 20}, "private-value", "at least 20 characters"),
    ({"maxItems": 1}, ["private-value", "other"], "at most 1 items"),
    ({"enum": ["private-contract-value"]}, "private-value", "registered enum"),
    ({"const": "private-contract-value"}, "private-value", "registered constant"),
    ({"pattern": "^private-contract-value$"}, "private-value", "registered pattern"),
    ({"format": "uuid"}, "private-value", "registered format"),
    ({"not": {"type": "string"}}, "private-value", "forbidden"),
    ({"oneOf": [{"type": "integer"}, {"type": "boolean"}]}, "private-value",
     "exactly one registered schema alternative"),
    ({"anyOf": [{"type": "integer"}]}, "private-value", "registered schema alternative"),
])
def test_schema_diagnostic_uses_contract_constraints_without_values(rule, value, expected):
    """位置と固定制約だけを返し、例外本文に含まれる原値を投影しない。"""
    schema = {"type": "object", "properties": {"limit": rule}}
    errors = Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
        {"limit": value}
    )
    message = schema_error_summary(errors)
    assert message.startswith("/limit:") and expected in message
    assert "private-value" not in message and "private-contract-value" not in message


def test_nested_schema_path_masks_dynamic_dictionary_keys():
    """配列位置は保ち、入力から来た辞書 key は契約上の property と区別する。"""
    schema = {
        "type": "object", "properties": {"rows": {"type": "array", "items": {
            "type": "object", "additionalProperties": {
                "type": "object", "properties": {"a/b~c": {"type": "integer"}},
            },
        }}},
    }
    errors = Draft202012Validator(schema).iter_errors({
        "rows": [{"private-key": {"a/b~c": "private-value"}}],
    })
    assert schema_error_summary(errors) == "/rows/0/*/a~1b~0c: expected integer"


def test_required_and_unknown_fields_do_not_reflect_values_or_unknown_names():
    """不足 field は契約から示し、余分な field 名や値を本文からコピーしない。"""
    schema = {"type": "object", "additionalProperties": False,
              "required": ["purpose", "directory"],
              "properties": {"purpose": {}, "directory": {}}}
    message = schema_error_summary(Draft202012Validator(schema).iter_errors({
        "private-key": "private-value",
    }))
    assert "missing required fields: purpose, directory" in message
    assert "additional fields are not allowed" in message
    assert "private-key" not in message and "private-value" not in message


def test_many_schema_errors_produce_a_bounded_summary():
    """多数の違反や長い property 名が、応答と監査のサイズを膨らませない。"""
    schema = {"type": "object", "properties": {
        "field" + str(index) + "x" * 200: {"type": "integer"} for index in range(20)
    }}
    message = schema_error_summary(Draft202012Validator(schema).iter_errors({
        key: "private-value" for key in schema["properties"]
    }))
    assert 0 < len(message) <= 280
    assert message.count("expected integer") <= 3
    assert "private-value" not in message
