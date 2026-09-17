"""Tool 失敗の分類・相関 ID と脱敏済みの MCP 診断データを投影する。"""

from __future__ import annotations

import re
from typing import Any

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
