"""Tool 失敗の分類・相関 ID と脱敏済みの MCP 診断データを投影する。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from jsonschema.exceptions import ValidationError

from skillmind.agent.database_errors import safe_database_diagnostic
from skillmind.integrations.mcp_errors import safe_remote_detail

MCP_REASONS = frozenset(
    {
        "configuration",
        "arguments",
        "invalid_arguments",
        "contract",
        "contract_changed",
        "desktop_busy",
        "remote_tool_error",
        "protocol_error",
        "result",
        "invalid_response",
        "transport_unconfirmed",
    }
)


def schema_error_summary(errors: Iterable[ValidationError]) -> str:
    """契約上の位置と制約を短く示し、入力値・正規表現・動的な辞書 key は返さない。"""
    details: list[str] = []
    for error in errors:
        detail = _describe(error)[:160]
        alternative = _closest_alternative(error)
        if alternative:
            # どの形を満たすべきかだけでは直せないため、最も近い形の不足も示す。
            detail += " (closest alternative: " + "; ".join(
                _describe(item)[:100] for item in alternative[:3]
            ) + ")"
        detail = detail[:270]
        if detail not in details:
            if len("; ".join([*details, detail])) > 280:
                break
            details.append(detail)
        if len(details) == 3:
            break
    return "; ".join(details)


def _closest_alternative(error: ValidationError) -> list[ValidationError]:
    """oneOf/anyOf の各 alternative のうち、違反が最も少ないものの違反を返す。"""
    if error.validator not in {"oneOf", "anyOf"} or not error.context:
        return []
    depth = len(error.absolute_schema_path)
    branches: dict[Any, list[ValidationError]] = {}
    for item in error.context:
        branches.setdefault(list(item.absolute_schema_path)[depth], []).append(item)
    return min(branches.values(), key=len)


def _describe(error: ValidationError) -> str:
    """一つの違反を「位置: 修正すべき制約」へ投影する。"""
    schema_path = list(error.absolute_schema_path)
    properties = {
        str(schema_path[index + 1])
        for index, part in enumerate(schema_path[:-1]) if part == "properties"
    }
    path = "/".join(
        _pointer_part(str(part)) if isinstance(part, int) or part in properties else "*"
        for part in error.absolute_path
    )
    location = ("/" + path)[:120] if path else "$"
    keyword, constraint = error.validator, error.validator_value
    if keyword == "required" and isinstance(error.instance, dict):
        missing = [_pointer_part(name) for name in constraint if name not in error.instance]
        rule = "missing required fields: " + ", ".join(missing[:3])
        if len(missing) > 3:
            rule += ", ..."
    elif keyword == "type":
        types = constraint if isinstance(constraint, list) else [constraint]
        rule = "expected " + " or ".join(
            value for value in types
            if value in {"object", "array", "string", "integer", "number", "boolean", "null"}
        )
    elif keyword in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}:
        operator = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">",
                    "exclusiveMaximum": "<"}[keyword]
        rule = f"must be {operator} {constraint} ({keyword})"
    elif keyword in {"minLength", "maxLength", "minItems", "maxItems",
                     "minProperties", "maxProperties"}:
        unit = "characters" if keyword.endswith("Length") else (
            "items" if keyword.endswith("Items") else "properties"
        )
        rule = f"must have {'at least' if keyword.startswith('min') else 'at most'} "
        rule += f"{constraint} {unit} ({keyword})"
    else:
        rule = {
            "additionalProperties": "additional fields are not allowed",
            "enum": "use a value from the registered enum",
            "const": "use the registered constant",
            "pattern": "must match the registered pattern",
            "format": "must match the registered format",
            "oneOf": "must match exactly one registered schema alternative",
            "anyOf": "must match a registered schema alternative",
            "not": "this value is forbidden by the registered schema",
            "uniqueItems": "items must be unique",
        }.get(keyword, "does not satisfy the registered schema constraint")
    return f"{location}: {rule}"


def _pointer_part(value: str) -> str:
    """契約に定義された field 名だけを表示可能な JSON Pointer token にする。"""
    return "".join(char for char in value if char.isprintable())[:48].replace(
        "~", "~0"
    ).replace("/", "~1")


def safe_tool_diagnostic(value: dict[str, Any]) -> dict[str, Any] | None:
    """種類ごとの allowlist を通し、MCP 本文は有界の脱敏処理を再適用する。"""
    if value.get("kind") != "mcp":
        return safe_database_diagnostic(value)
    if not isinstance(value.get("reason"), str) or value["reason"] not in MCP_REASONS:
        return None
    result = {"kind": "mcp", "reason": value["reason"]}
    diagnostic_id = value.get("diagnostic_id")
    if isinstance(diagnostic_id, str) and re.fullmatch(r"[0-9a-f]{32}", diagnostic_id):
        result["diagnostic_id"] = diagnostic_id
    local_id = value.get("local_diagnostic_id")
    if isinstance(local_id, str) and re.fullmatch(r"[0-9a-f]{32}", local_id):
        result["local_diagnostic_id"] = local_id
    if isinstance(value.get("remote_detail"), dict):
        result["remote_detail"] = safe_remote_detail(value["remote_detail"])
    return result
