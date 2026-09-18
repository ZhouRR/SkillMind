"""工具名によらない Schema 処理と、権限を cache しない MCP hot path を検証する。"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator
from skillmind.integrations import mcp_schema, mcp_tools


def catalog(count=40):
    """同じ一般契約を持つ多数の工具。特定 Runner の vocabulary は使用しない。"""
    return {
        "server": {"name": "Example", "version": "1"},
        "tools": [
            {
                "name": f"operation_{n}",
                "description": "Generic operation",
                "input_schema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
                "output_schema": None,
            }
            for n in range(count)
        ],
    }


@pytest.fixture(autouse=True)
def empty_caches():
    """テスト間で cache 成績と可変 fixture を共有しない。"""
    mcp_schema._compiled_schema.cache_clear()
    mcp_tools._normalized_catalog.cache_clear()
    yield
    mcp_schema._compiled_schema.cache_clear()
    mcp_tools._normalized_catalog.cache_clear()


def test_catalog_and_values_validate_each_identical_schema_once(monkeypatch):
    """40 工具を 10 回解決しても meta-schema 検証を 400 回再実行しない。"""
    checked = MagicMock(wraps=Draft202012Validator.check_schema)
    monkeypatch.setattr(Draft202012Validator, "check_schema", checked)
    config = {
        "tool_profile": mcp_tools.PROFILE,
        "tool_catalog": catalog(),
        "tool_permissions": {f"operation_{n}": "read" for n in range(40)},
    }
    scope = {"tool_names": list(config["tool_permissions"])}
    for _ in range(10):
        for tool in mcp_tools.configured_tools(config, scope):
            mcp_tools.validate_tool_value(tool["input_schema"], {"count": 1})
    assert checked.call_count == 1
    assert mcp_tools._normalized_catalog.cache_info().misses == 1


def test_cache_returns_detached_catalog_and_tool_values():
    """呼出し元の変更で、次の要求が使用する検証済み契約を壊さない。"""
    raw = catalog(1)
    original = deepcopy(raw)
    first = mcp_tools.normalize_catalog(raw)
    first["tools"][0]["input_schema"]["properties"]["count"]["type"] = "string"
    assert mcp_tools.normalize_catalog(raw) == original
    raw["tools"][0]["description"] = "New contract"
    assert mcp_tools.normalize_catalog(raw)["tools"][0]["description"] == "New contract"
    assert mcp_tools._normalized_catalog.cache_info().misses == 2


def test_cached_contract_never_caches_scope_or_admin_permission():
    """catalog が同じでも、現在の scope/permission が無ければ工具を返さない。"""
    config = {
        "tool_profile": mcp_tools.PROFILE,
        "tool_catalog": catalog(1),
        "tool_permissions": {"operation_0": "read"},
    }
    scope = {"tool_names": ["operation_0"]}
    mcp_tools.configured_tool(config, scope, "operation_0")
    with pytest.raises(ValueError):
        mcp_tools.configured_tool(config, {"tool_names": []}, "operation_0")
    config["tool_permissions"] = {}
    with pytest.raises(ValueError):
        mcp_tools.configured_tool(config, scope, "operation_0")


def test_validated_schema_is_content_addressed_not_object_addressed():
    """同じ dict を編集した時も、新しい契約で検証する。"""
    schema = {"type": "integer"}
    mcp_tools.validate_tool_value(schema, 1)
    schema["type"] = "string"
    with pytest.raises(ValueError):
        mcp_tools.validate_tool_value(schema, 1)
    mcp_tools.validate_tool_value(schema, "new")


@pytest.mark.parametrize(
    "schema,value",
    [
        (
            {
                "type": "object",
                "$defs": {"count": {"type": "integer", "minimum": 1}},
                "properties": {"n": {"$ref": "#/$defs/count"}},
                "required": ["n"],
            },
            {"n": 2},
        ),
        (
            {
                "type": "object",
                "definitions": {"a/b": {"type": "boolean"}},
                "properties": {"ok": {"$ref": "#/definitions/a~1b"}},
            },
            {"ok": True},
        ),
        (
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": False,
            },
            ["x", 1],
        ),
        (
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "array",
                "items": [{"type": "integer"}],
                "additionalItems": False,
            },
            [1],
        ),
        ({"if": {"type": "integer"}, "then": {"minimum": 0}, "else": {"type": "string"}}, 2),
        ({"type": "object", "default": {"$ref": "literal-business-data"}}, {}),
    ],
)
def test_standard_local_schema_shapes_are_not_vendor_specific(schema, value):
    """local $defs、条件、tuple を原 dialect で処理する。"""
    mcp_tools.validate_tool_value(schema, value)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/schema"},
        {"$ref": "#/missing"},
        {"$defs": {"self": {"$ref": "#/$defs/self"}}, "$ref": "#/$defs/self"},
        {"$schema": "https://example.invalid/dialect", "type": "object"},
    ],
)
def test_network_missing_and_cyclic_references_are_never_fetched(schema):
    """ネットワーク取得や循環を許可して通用性を作らない。"""
    with pytest.raises(ValueError):
        mcp_schema.validate_schema(schema)


def test_reference_constraints_still_validate_actual_arguments():
    """local reference を許可しても、型・値の検証を省略しない。"""
    schema = {"$defs": {"n": {"type": "integer", "minimum": 2}}, "$ref": "#/$defs/n"}
    for value in ("2", True, 1):
        with pytest.raises(ValueError):
            mcp_tools.validate_tool_value(schema, value)


def test_query_path_does_not_enumerate_all_authorized_tools(monkeypatch):
    """selected tool の解決は一覧 API の全件 projection を呼び出さない。"""
    forbidden = MagicMock(side_effect=AssertionError("unexpected full projection"))
    monkeypatch.setattr(mcp_tools, "configured_tools", forbidden)
    config = {
        "tool_profile": mcp_tools.PROFILE,
        "tool_catalog": catalog(),
        "tool_permissions": {"operation_3": "read"},
    }
    tool = mcp_tools.configured_tool(config, {"tool_names": ["operation_3"]}, "operation_3")
    assert tool["name"] == "operation_3"
    forbidden.assert_not_called()


def test_repeated_reference_graph_cannot_expand_without_a_bound():
    """小さい入力の指数参照を compile 時に止め、live tool 実行へ持ち込まない。"""
    definitions = {"leaf": {"type": "integer"}}
    previous = "leaf"
    for level in range(5):
        name = f"level_{level}"
        definitions[name] = {"allOf": [{"$ref": f"#/$defs/{previous}"}] * 20}
        previous = name
    with pytest.raises(ValueError):
        mcp_schema.validate_schema({"$defs": definitions, "$ref": f"#/$defs/{previous}"})


def test_local_reference_readback_is_verified_at_runtime_without_false_static_rejection():
    """部分射影で参照を誤解しないが、全出力と実 read-back の違反は拒否する。"""
    from skillmind.effects.mcp_call import check_read_back
    from skillmind.integrations.mcp_readback import McpReadBackError, validate_read_back_schema

    schema = {
        "$defs": {"state": {"enum": ["ready", "pending"]}},
        "type": "object",
        "properties": {"state": {"$ref": "#/$defs/state"}},
        "required": ["state"],
    }
    checks = [{"path": "/state", "equals": "ready"}]
    validate_read_back_schema(schema, checks)
    mcp_tools.validate_tool_value(schema, {"state": "ready"})
    with pytest.raises(ValueError):
        mcp_tools.validate_tool_value(schema, {"state": "invented"})
    with pytest.raises(McpReadBackError):
        check_read_back({"checks": checks}, {"state": "pending"})


@pytest.mark.parametrize(
    "tuple_schema",
    [
        {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}], "items": False},
        {
            "type": "array",
            "items": [{"type": "string"}, {"type": "integer"}],
            "additionalItems": False,
        },
    ],
)
def test_standard_tuple_readback_uses_actual_position_schema(tuple_schema):
    """prefixItems と旧 dialect の tuple を追加要素の禁止と混同しない。"""
    from skillmind.integrations.mcp_readback import McpReadBackError, validate_read_back_schema

    schema = {
        "type": "object",
        "properties": {"result": tuple_schema},
        "additionalProperties": False,
    }
    validate_read_back_schema(
        schema, [{"path": "/result/0", "equals": "value"}, {"path": "/result/1", "equals": 1}]
    )
    for check in ({"path": "/result/0", "equals": 1}, {"path": "/result/2", "equals": "extra"}):
        with pytest.raises(McpReadBackError):
            validate_read_back_schema(schema, [check])
