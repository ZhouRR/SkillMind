"""MCP の有界 Schema を内容単位で検証し、工具名に依存せず再利用する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, cast
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

from skillmind.core.hashing import canonical_json

MAX_SCHEMA_BYTES = 262_144
# 制約を黙って無視しない。外部取得・動的参照・任意正規表現はこの in-process 境界で開かない。
_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "description",
        "title",
        "default",
        "format",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "uniqueItems",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "$schema",
        "$defs",
        "definitions",
        "$ref",
        "$comment",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "if",
        "then",
        "else",
        "contains",
        "minContains",
        "maxContains",
        "prefixItems",
        "additionalItems",
        "propertyNames",
        "dependentRequired",
        "dependentSchemas",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)
_MAPS = frozenset({"properties", "$defs", "definitions", "dependentSchemas"})
_ARRAYS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SINGLE = frozenset(
    {
        "items",
        "additionalItems",
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)


def _local_reference(root: Any, ref: Any) -> Any:
    """同じ文書の JSON Pointer のみ解決し、URL や他ファイルへ移動しない。"""
    if not isinstance(ref, str) or len(ref) > 1024 or not ref.startswith("#"):
        raise ValueError("MCP schema references must stay inside the document")
    pointer = unquote(ref[1:])
    if pointer == "":
        return root
    if not pointer.startswith("/"):
        raise ValueError("MCP schema requires a local JSON Pointer")
    value = root
    try:
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not token.isascii() or not token.isdecimal() or str(int(token)) != token:
                    raise ValueError("Invalid schema pointer index")
                value = value[int(token)]
            else:
                value = value[token]
    except (KeyError, IndexError, TypeError):
        raise ValueError("MCP schema reference is missing") from None
    return value


def _check_structure(root: Any) -> None:
    """Schema keyword だけを巡回し、default/enum の業務 JSON を参照と誤認しない。"""
    active: set[int] = set()
    visited_nodes = 0

    def visit(value: Any, depth: int) -> None:
        """局所参照の循環とサイズを確認し、meta-schema 検証自体は根で一回だけ行う。"""
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > 100_000:
            raise ValueError("MCP schema reference expansion exceeds its limit")
        if isinstance(value, bool):
            return
        if not isinstance(value, dict) or depth > 12 or len(value) > 64:
            raise ValueError("MCP tool schema is invalid")
        identity = id(value)
        if identity in active:
            raise ValueError("MCP schema references must not be cyclic")
        if set(value) - _KEYWORDS:
            raise ValueError("MCP tool schema uses unsupported constraints or references")
        active.add(identity)
        if "$ref" in value:
            visit(_local_reference(root, value["$ref"]), depth + 1)
        for key in _MAPS & value.keys():
            children = value[key]
            if not isinstance(children, dict) or len(children) > 100:
                raise ValueError("MCP schema properties are invalid")
            for child in children.values():
                visit(child, depth + 1)
        for key in _ARRAYS & value.keys():
            children = value[key]
            if not isinstance(children, list) or len(children) > 30:
                raise ValueError("MCP schema alternatives exceed their limit")
            for child in children:
                visit(child, depth + 1)
        for key in _SINGLE & value.keys():
            child = value[key]
            if key == "items" and isinstance(child, list):
                if len(child) > 30:
                    raise ValueError("MCP tuple schema exceeds its limit")
                for item in child:
                    visit(item, depth + 1)
            else:
                visit(child, depth + 1)
        active.remove(identity)

    visit(root, 0)


def _schema_key(schema: Any) -> str:
    """可変 dict の identity でなく、現在の全内容を cache key にする。"""
    encoded = canonical_json(schema)
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError("MCP schema exceeds its limit")
    return encoded


@lru_cache(maxsize=64)
def _compiled_schema(encoded: str) -> Validator:
    """cache は schema の私有コピーだけを保持し、権限・値・credential は保存しない。"""
    schema = json.loads(encoded)
    _check_structure(schema)
    cls = validator_for(
        schema,
        default=None
        if isinstance(schema, Mapping) and "$schema" in schema
        else Draft202012Validator,
    )
    if cls is None:
        raise ValueError("MCP schema dialect is not supported")
    try:
        cls.check_schema(schema)
    except SchemaError:
        raise ValueError("MCP tool schema is invalid") from None
    return cast(Validator, cls(schema, format_checker=FormatChecker(), registry=Registry()))


def validate_schema(schema: Any) -> None:
    """登録時と実行時が同じ Schema validator を使用する。"""
    _compiled_schema(_schema_key(schema))


def validate_value(schema: Any, value: Any) -> None:
    """入力と structured output を原 Schema で確認する。外部 reference は取得しない。"""
    validator = _compiled_schema(_schema_key(schema))
    try:
        valid = validator.is_valid(value)
    except (Unresolvable, RecursionError):
        raise ValueError("MCP schema could not be evaluated") from None
    if not valid:
        raise ValueError("MCP value does not match the frozen tool schema")
