"""MCP の保存済み Schema と管理者の工具別権限を検証する。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry

from skillmind.core.hashing import canonical_json, sha256_hex

PROFILE = "mcp-tools/v1"
MAX_ARGUMENT_BYTES = 65_536


class McpResultError(ValueError):
    """遠端失敗と不正な応答を分離し、本文ではなく診断 ID だけを保持する。"""

    def __init__(self, reason: str, diagnostic_id: str | None = None) -> None:
        """資格情報を含み得る遠端メッセージは保持しない。"""
        super().__init__(reason)
        self.reason = reason
        self.diagnostic_id = diagnostic_id


def digest(value: Any) -> str:
    """契約・観測の canonical JSON を原形のまま識別する。"""
    return "sha256:" + sha256_hex(canonical_json(value))


def _schema(value: Any, depth: int = 0) -> None:
    """外部参照なしの有界 Schema を検証する。"""
    if isinstance(value, bool):
        return
    if not isinstance(value, dict) or depth > 12 or len(value) > 30:
        raise ValueError("MCP tool schema is invalid")
    allowed = {
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
        "maxItems", "minProperties", "maxProperties", "uniqueItems",
        "anyOf", "oneOf", "allOf", "not", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "$schema",
    }
    if set(value) - allowed:
        raise ValueError("MCP tool schema uses unsupported constraints or references")
    props = value.get("properties", {})
    if not isinstance(props, dict) or len(props) > 100:
        raise ValueError("MCP tool schema properties are invalid")
    for child in props.values():
        _schema(child, depth + 1)
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in value:
            if not isinstance(value[keyword], list) or len(value[keyword]) > 30:
                raise ValueError("MCP schema alternatives exceed their limit")
            for child in value[keyword]:
                _schema(child, depth + 1)
    for key in ("items", "additionalProperties", "not"):
        if key in value:
            _schema(value[key], depth + 1)
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError:
        raise ValueError("MCP tool schema is invalid") from None


def normalize_catalog(value: Any) -> dict[str, Any]:
    """サービスと Schema の snapshot を検証し、注釈から実行権を生成しない。"""
    if not isinstance(value, dict) or set(value) != {"server", "tools"}:
        raise ValueError("MCP tool catalog is invalid")
    if len(canonical_json(value).encode()) > 262_144:
        raise ValueError("MCP tool catalog exceeds its limit")
    server, entries = value["server"], value["tools"]
    if (
        not isinstance(server, dict)
        or set(server) != {"name", "version"}
        or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in server.values())
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 100
    ):
        raise ValueError("MCP server or tools are invalid")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name",
            "description",
            "input_schema",
            "output_schema",
        }:
            raise ValueError("MCP tool descriptor is invalid")
        name = entry["name"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name)
            or name in names
        ):
            raise ValueError("MCP tool name is invalid or duplicated")
        names.add(name)
        if not isinstance(entry["description"], str) or len(entry["description"]) > 4000:
            raise ValueError("MCP tool description exceeds its limit")
        _schema(entry["input_schema"])
        if entry["output_schema"] is not None:
            _schema(entry["output_schema"])
    return {"server": dict(server), "tools": sorted(entries, key=lambda item: item["name"])}


def configured_tool(
    config: Mapping[str, Any], scope: Mapping[str, Any], name: str
) -> dict[str, Any]:
    """管理者が保存した契約と、原 binding の工具範囲を同時に要求する。"""
    if config.get("tool_profile") != PROFILE or name not in scope.get("tool_names", []):
        raise ValueError("MCP tool is outside the configured scope")
    if tool_access(config, name) not in {"read", "call"}:
        raise ValueError("MCP tool permission is missing")
    for entry in normalize_catalog(config.get("tool_catalog"))["tools"]:
        if entry["name"] == name:
            return dict(entry)
    raise ValueError("MCP tool is missing from the frozen catalog")


def tool_access(config: Mapping[str, Any], name: str) -> str | None:
    """遠端の readOnlyHint は認可に使用しない。"""
    permissions = config.get("tool_permissions", {})
    return permissions.get(name) if isinstance(permissions, dict) else None


def validate_arguments(name: str, arguments: Any, *, proposal: bool = False) -> dict[str, Any]:
    """具体的な引数は保存 Schema に委ね、共通の大きさだけを制限する。"""
    if (not isinstance(arguments, dict)
        or len(canonical_json(arguments).encode()) > MAX_ARGUMENT_BYTES):
        raise ValueError("MCP arguments are invalid or too large")
    frozen: dict[str, Any] = json.loads(canonical_json(arguments))
    return frozen


def validate_tool_value(schema: dict[str, Any], value: Any) -> None:
    """外部参照のない小さい Schema で呼出し入力・structured output を照合する。"""
    _schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry())
    if not validator.is_valid(value):
        raise ValueError("MCP value does not match the frozen tool schema")


def parse_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """構造化内容を優先し、その他の内容も取得済みデータとして返す。"""
    if value.get("is_error") is True:
        # FlaUI の診断参照のみ許可する。例外本文・path・stack は反射しない。
        ids = set()
        content = value.get("content")
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                ids.update(re.findall(r"\[diagnosticId=([0-9a-f]{32})\]", item["text"][:65_536]))
        raise McpResultError("remote_tool_error", next(iter(ids)) if len(ids) == 1 else None)
    if value.get("is_error") is not False:
        raise McpResultError("invalid_response")
    if len(canonical_json(value).encode()) > 1_048_576:
        raise ValueError("MCP result exceeds its limit")
    structured = value.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = value.get("content")
    if not isinstance(content, list) or len(content) > 100:
        raise McpResultError("invalid_response")
    if len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text":
        try:
            decoded = json.loads(content[0]["text"])
            if isinstance(decoded, dict):
                return decoded
        except (ValueError, TypeError, KeyError):
            pass
    # URI・画像・添付はその場で取得せず、返された内容だけを証拠にする。
    return {"content": content}
