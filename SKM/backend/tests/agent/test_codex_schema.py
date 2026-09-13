"""Codex 転送形式と元の業務 JSON の可逆性を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from skillmind.agent.codex_diagnostics import codex_failure_detail
from skillmind.agent.codex_schema import CodexOutputError, CodexOutputSchema
from skillmind.skills.interpreter import build_interpreter_generation_schema


def assert_strict_schema(schema: dict[str, Any], depth: int = 0) -> None:
    """合成 endpoint でも native 出力の構造制約を検査する。"""

    assert depth <= 10
    assert set(schema) <= {
        "type", "properties", "required", "additionalProperties", "items", "enum",
        "anyOf", "description", "pattern",
    }
    if "anyOf" in schema:
        for child in schema["anyOf"]:
            assert_strict_schema(child, depth)
        return
    assert "type" in schema
    if schema["type"] == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for child in schema["properties"].values():
            assert_strict_schema(child, depth + 1)
    if schema["type"] == "array":
        assert_strict_schema(schema["items"], depth + 1)


def _closed(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Fixture の自由 key を閉じ、optional 差分を明示する。"""

    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def test_real_interpreter_schema_is_strict_and_unchanged() -> None:
    """実生成 Schema の typeless const、自由値、optional、再帰を一緒に検査する。"""

    original = build_interpreter_generation_schema(
        Path(__file__).resolve().parents[3] / "contracts"
    )
    before = deepcopy(original)
    codec = CodexOutputSchema(original)
    assert_strict_schema(codec.schema)
    Draft202012Validator.check_schema(codec.schema)
    assert codec.schema["properties"]["response_version"]["type"] == "string"
    assert original == before


def test_optional_null_open_objects_and_union_preserve_values() -> None:
    """省略と明示 null を区別し、自由 object の key と型を失わない。"""

    original = _closed({
        "version": {"const": "v1"}, "optional": {"type": "string"},
        "nullable": {"type": ["null", "string"]}, "null_only": {"type": "null"},
        "free": {"type": "object"},
        "union": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
    }, ["version", "free", "union"])
    codec = CodexOutputSchema(original)
    wire = {"version": "v1", "optional": None,
            "nullable": {"present": True, "value": None},
            "null_only": {"present": True, "value": None},
            "free": json.dumps({"日本語": [True, 1, None, {"key": "value"}]}), "union": "42"}
    decoded = codec.decode(json.dumps(wire))
    assert decoded == {"version": "v1", "nullable": None, "null_only": None,
                       "free": {"日本語": [True, 1, None, {"key": "value"}]}, "union": 42}
    Draft202012Validator(original).validate(decoded)
    wire["nullable"] = {"present": False, "value": None}
    wire["null_only"] = {"present": False, "value": None}
    assert "nullable" not in codec.decode(json.dumps(wire))
    assert "null_only" not in codec.decode(json.dumps(wire))


def test_recursive_local_ref_and_root_wrapper() -> None:
    """参照 key の escaping と再帰退避、自由 root の復元を検査する。"""

    original = _closed({"tree": {"$ref": "#/$defs/a~1b"}}, ["tree"])
    original["$defs"] = {"a/b": _closed({"next": {"$ref": "#/$defs/a~1b"}}, [])}
    codec = CodexOutputSchema(original)
    assert_strict_schema(codec.schema)
    decoded = codec.decode(json.dumps({"tree": {"next": '{"next":{}}'}}))
    assert decoded == {"tree": {"next": {"next": {}}}}
    Draft202012Validator(original).validate(decoded)
    free = CodexOutputSchema({"type": "object"})
    assert free.decode(json.dumps({"result_json": '{"ok":true}'})) == {"ok": True}


@pytest.mark.parametrize("text", [
    json.dumps({"result_json": value}) for value in ["NaN", '{"x":1,"x":2}', "[]", "{"]
] + ['{}', '{"result_json":"{}","extra":1}', '{"result_json":"{}","result_json":"{}"}'])
def test_invalid_wire_or_encoded_json_is_rejected(text: str) -> None:
    """欠落・余分・不正 JSON を黙示的に業務値へ変換しない。"""

    with pytest.raises(CodexOutputError):
        CodexOutputSchema({"type": "object"}).decode(text)


def test_restored_candidate_still_requires_original_validation() -> None:
    """Wire 側で表せない制約を元の validator が引き続き拒否する。"""

    original = _closed({"name": {"type": "string", "minLength": 3}}, ["name"])
    candidate = CodexOutputSchema(original).decode('{"name":"x"}')
    assert not Draft202012Validator(original).is_valid(candidate)


def test_diagnostics_only_expose_known_classification() -> None:
    """Provider 本文と未知値に資格情報があっても分類以外を返さない。"""

    error = {"message": json.dumps({"status": 400, "error": {
        "type": "invalid_request_error", "code": "invalid_json_schema",
        "message": "secret fixture content", "param": "secret fixture header",
    }})}
    assert codex_failure_detail(error) == "codex:invalid_json_schema; http_status=400"
    assert codex_failure_detail({"code": "secret", "message": "secret"}) == "codex:unknown"


def test_depth_limit_only_encodes_containers_and_reports_bad_leaf_path() -> None:
    """深さ上限でも scalar は native 型を保ち、JSON 自由値の誤り位置を返す。"""

    original = _closed({"free": {"type": "object"}, "name": {"type": "string"}}, ["name", "free"])
    value: dict[str, Any] = {"name": "plain text", "free": "{"}
    for _ in range(5):
        original = _closed({"child": original}, ["child"])
        value = {"child": value}
    codec = CodexOutputSchema(original)
    node = codec.schema
    for _ in range(5):
        node = node["properties"]["child"]
    assert node["properties"]["name"] == {"type": "string"}
    with pytest.raises(CodexOutputError) as caught:
        codec.decode(json.dumps(value))
    assert caught.value.detail == (
        "codex_output:invalid_json; path=/child/child/child/child/child/free"
    )


def test_wire_validation_reports_position_without_candidate_value() -> None:
    """型違反の実値を含めず、元 Schema 上の位置だけを返す。"""

    codec = CodexOutputSchema(_closed({"ok": {"type": "boolean"}}, ["ok"]))
    with pytest.raises(CodexOutputError) as caught:
        codec.decode('{"ok":"private fixture content"}')
    assert caught.value.detail == "codex_output:invalid_wire_type; path=/ok"


def test_required_nullable_scalar_keeps_native_value() -> None:
    """必須 nullable path/line を二重 JSON にせず、文字列・整数・null を保つ。"""

    original = _closed({
        "path": {"type": ["string", "null"]},
        "line": {"type": ["integer", "null"]},
    }, ["path", "line"])
    codec = CodexOutputSchema(original)
    assert codec.schema["properties"]["path"] == {"type": ["string", "null"]}
    assert codec.decode('{"path":"SKILL.md","line":12}') == {"path": "SKILL.md", "line": 12}
    assert codec.decode('{"path":null,"line":null}') == {"path": None, "line": None}


def test_optional_nullable_text_distinguishes_omission_null_and_literal_null() -> None:
    """source_section の普通文字列、文字列 null、明示 null、省略を可逆に保つ。"""

    codec = CodexOutputSchema(_closed({"source_section": {"type": ["string", "null"]}}, []))
    for value in ["SKILL.md", "null", "", None]:
        wire = {"source_section": {"present": True, "value": value}}
        assert codec.decode(json.dumps(wire)) == {"source_section": value}
    assert codec.decode('{"source_section":{"present":false,"value":null}}') == {}


def test_encoded_object_rejects_extra_string_wrapper_at_wire_boundary() -> None:
    """自由 object を単なる説明文や二重 JSON 文字列として生成させない。"""

    codec = CodexOutputSchema(_closed({"extensions": {"type": "object"}}, ["extensions"]))
    assert codec.decode(json.dumps({"extensions": "{}"})) == {"extensions": {}}
    for value in ['"{}"', '"description"', '[]']:
        with pytest.raises(CodexOutputError) as caught:
            codec.decode(json.dumps({"extensions": value}))
        assert caught.value.detail == "codex_output:invalid_wire_pattern; path=/extensions"
