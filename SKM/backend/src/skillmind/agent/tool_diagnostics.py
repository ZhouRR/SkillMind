"""Tool 失敗の監査情報を固定分類と非秘密の相関 ID へ限定する。"""

from __future__ import annotations

import re
from typing import Any

from skillmind.agent.database_errors import safe_database_diagnostic

MCP_REASONS = frozenset(
    {
        "configuration",
        "arguments",
        "invalid_arguments",
        "contract",
        "contract_changed",
        "desktop_busy",
        "remote_tool_error",
        "result",
        "invalid_response",
        "transport_unconfirmed",
    }
)


def safe_tool_diagnostic(value: dict[str, Any]) -> dict[str, Any] | None:
    """本文・SQL・credential は受け取らず、種類ごとの allowlist を通す。"""
    if value.get("kind") != "mcp":
        return safe_database_diagnostic(value)
    if not isinstance(value.get("reason"), str) or value["reason"] not in MCP_REASONS:
        return None
    result = {"kind": "mcp", "reason": value["reason"]}
    diagnostic_id = value.get("diagnostic_id")
    if isinstance(diagnostic_id, str) and re.fullmatch(r"[0-9a-f]{32}", diagnostic_id):
        result["diagnostic_id"] = diagnostic_id
    return result
