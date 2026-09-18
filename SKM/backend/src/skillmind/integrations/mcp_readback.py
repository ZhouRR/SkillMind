"""MCP 回読条件を出力 Schema と照合し、送信前に確定できる矛盾を拒否する。"""

from __future__ import annotations

import re
from typing import Any

from skillmind.integrations.mcp_tools import validate_tool_value


class McpReadBackError(ValueError):
    """外部値や path 本文を持たず、承認条件の位置と失敗分類を保持する。"""

    def __init__(self, reason: str, check_index: int) -> None:
        """条件の値は元提案を参照し、例外へ反射しない。"""
        self.reason, self.check_index = reason, check_index
        super().__init__(f"MCP read_back check {check_index + 1}: {reason}")


def validate_read_back_schema(schema: Any, checks: list[dict[str, Any]]) -> None:
    """可能な枝を残し、存在し得ない path と比較値だけを拒否する。"""
    if schema is None:
        # 未宣言は一致の証明ではないが、既存の観測ベースの回読を禁止しない。
        return
    for index, check in enumerate(checks):
        parts = [
            part.replace("~1", "/").replace("~0", "~") for part in check["path"][1:].split("/")
        ]
        values = check["one_of"] if "one_of" in check else [check["equals"]]
        if not any(_possible(schema, parts, value) for value in values):
            raise McpReadBackError("output_schema_conflict", index)


def _possible(schema: Any, parts: list[str], value: Any) -> bool:
    """任意属性・union は保守的に許し、閉じた object/array の不可能条件を検出する。"""
    if schema is False:
        return False
    if not isinstance(schema, dict):
        return True
    if "$ref" in schema:
        # 部分射影だけでは元文書の reference を解決できない。ここで不可能と断定せず、
        # 実応答は Source の全 outputSchema と exact read-back の両方で検証する。
        return True
    if not parts:
        try:
            validate_tool_value(schema, value)
            return True
        except ValueError:
            return False
    # oneOf の枝の重なりや not は部分射影だけでは判断できないため、送信前に過剰拒否しない。
    if any(not _possible(branch, parts, value) for branch in schema.get("allOf", [])):
        return False
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema and not any(_possible(b, parts, value) for b in schema[keyword]):
            return False
    types = schema.get("type", ["object", "array"])
    types = [types] if isinstance(types, str) else types
    key, rest = parts[0], parts[1:]
    if "object" in types:
        child = schema.get("properties", {}).get(key, schema.get("additionalProperties", True))
        if _possible(child, rest, value):
            return True
    if (
        "array" in types
        and re.fullmatch(r"0|[1-9][0-9]*", key)
        and ("maxItems" not in schema or int(key) < schema["maxItems"])
    ):
        index = int(key)
        prefix = schema.get("prefixItems", [])
        items = schema.get("items", True)
        if isinstance(prefix, list) and index < len(prefix):
            child = prefix[index]
        elif isinstance(items, list):
            child = items[index] if index < len(items) else schema.get("additionalItems", True)
        else:
            child = items
        return _possible(child, rest, value)
    return False
