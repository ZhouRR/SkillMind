"""MCP の発見契約と、審査済み単独操作 profile の境界を定義する。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry

from skillmind.core.hashing import canonical_json, sha256_hex

PROFILE = "flaui-step/v1"
READ_TOOLS = frozenset({"inspect_window", "get_step_status"})
WRITE_TOOLS = frozenset({"open_application", "execute_step", "cancel_step"})
TOOLS = READ_TOOLS | WRITE_TOOLS
MAX_ARGUMENT_BYTES = 65_536


def digest(value: Any) -> str:
    """契約・観測の canonical JSON を原形のまま識別する。"""
    return "sha256:" + sha256_hex(canonical_json(value))


def _schema(value: Any, depth: int = 0) -> None:
    """最初の profile が使用する有界の非再帰 Schema だけを許可する。"""
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
        "maxItems",
    }
    if set(value) - allowed or value.get("format", "uuid") != "uuid":
        raise ValueError("MCP tool schema uses unsupported constraints or references")
    props = value.get("properties", {})
    if not isinstance(props, dict) or len(props) > 100:
        raise ValueError("MCP tool schema properties are invalid")
    for child in props.values():
        _schema(child, depth + 1)
    for key in ("items", "additionalProperties"):
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
    """元の profile、tool 許可と保存済み契約の三つを同時に要求する。"""
    if (
        config.get("tool_profile") != PROFILE
        or name not in TOOLS
        or name not in scope.get("tool_names", [])
    ):
        raise ValueError("MCP tool is outside the configured profile or scope")
    catalog = normalize_catalog(config.get("tool_catalog"))
    if catalog["server"]["name"] != "FlaUiMcp" or not catalog["server"]["version"].startswith(
        "0.3."
    ):
        raise ValueError("MCP server is not supported by the configured profile")
    for entry in catalog["tools"]:
        if entry["name"] == name:
            return dict(entry)
    raise ValueError("MCP tool is missing from the frozen catalog")


def validate_arguments(name: str, arguments: Any, *, proposal: bool = False) -> dict[str, Any]:
    """FlaUI の外部変更を、有界な登録 app と既知の一操作だけに限定する。"""
    if (
        not isinstance(arguments, dict)
        or len(canonical_json(arguments).encode()) > MAX_ARGUMENT_BYTES
    ):
        raise ValueError("MCP arguments are invalid or too large")
    required = {
        "open_application": {"appId"},
        "inspect_window": {"appId"},
        "get_step_status": {"requestId"},
        "cancel_step": {"requestId"},
        "execute_step": {"appId", "operation", "inputsJson"}
        | (set() if proposal else {"requestId"}),
    }.get(name)
    optional = (
        {"windowTitle"}
        if name == "inspect_window"
        else ({"windowTitle", "timeoutSeconds"} if name == "execute_step" else set())
    )
    if required is None or not required <= arguments.keys() or set(arguments) - required - optional:
        raise ValueError("MCP arguments do not match the operation")
    for key in ("appId", "requestId", "operation", "inputsJson", "windowTitle"):
        if (
            key in arguments
            and not (key == "windowTitle" and arguments[key] is None)
            and (
                not isinstance(arguments[key], str)
                or not 1 <= len(arguments[key]) <= MAX_ARGUMENT_BYTES
            )
        ):
            raise ValueError("MCP argument has an invalid type")
    if "requestId" in arguments and str(UUID(arguments["requestId"])) != arguments["requestId"]:
        raise ValueError("MCP request ID must be a canonical UUID")
    if name == "execute_step":
        seconds = arguments.get("timeoutSeconds", 15)
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise ValueError("MCP operation timeout must be between 1 and 60 seconds")
        operation = arguments["operation"]
        if operation not in {
            "click",
            "set_text",
            "read_text",
            "assert_text",
            "select_row",
            "wait_for_element",
            "screenshot",
        }:
            raise ValueError("MCP operation is not supported")
        value = json.loads(arguments["inputsJson"])
        if not isinstance(value, dict) or set(value) - {"automationId", "name", "text", "cells"}:
            raise ValueError("MCP operation inputs are invalid")
        if operation == "screenshot":
            if value:
                raise ValueError("MCP screenshot requires empty inputs")
        elif (
            sum(isinstance(value.get(k), str) and bool(value[k]) for k in ("automationId", "name"))
            != 1
        ):
            raise ValueError("MCP operation requires one observed control selector")
        selectors = set(value) & {"automationId", "name"}
        extras = (
            {"text"}
            if operation in {"set_text", "assert_text"}
            else {"cells"}
            if operation == "select_row"
            else set()
        )
        if operation != "screenshot" and (len(selectors) != 1 or set(value) != selectors | extras):
            raise ValueError("MCP control inputs do not match the operation")
        if "text" in extras and not isinstance(value["text"], str):
            raise ValueError("MCP operation text is invalid")
        if "cells" in extras and (
            not isinstance(value["cells"], dict)
            or not value["cells"]
            or any(not isinstance(v, str) for v in value["cells"].values())
        ):
            raise ValueError("MCP row selectors are invalid")
    frozen: dict[str, Any] = json.loads(canonical_json(arguments))
    return frozen


def validate_tool_value(schema: dict[str, Any], value: Any) -> None:
    """外部参照のない小さい Schema で呼出し入力・structured output を照合する。"""
    _schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry())
    if not validator.is_valid(value):
        raise ValueError("MCP value does not match the frozen tool schema")


def parse_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """FlaUI の JSON text を有界に読み、MCP 成功と操作成功を混同しない。"""
    if value.get("is_error") is not False:
        raise ValueError("MCP tool returned an error")
    if len(canonical_json(value).encode()) > 1_048_576:
        raise ValueError("MCP result exceeds its limit")
    content = value.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "text"
    ):
        raise ValueError("MCP tool did not return the profile result")
    result = json.loads(content[0]["text"])
    if not isinstance(result, dict):
        raise ValueError("MCP tool result is not an object")
    canonical_json(result)
    return result
