"""MCP の固定失敗分類と脱敏済みの遠端診断を区別する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from skillmind.effects.domain import EffectEvidenceDraft
from skillmind.effects.redmine import EffectProviderTransportError
from skillmind.integrations.mcp_errors import (
    local_diagnostic_id,
    remote_detail_message,
    safe_remote_detail,
)

STAGES = frozenset({"preflight", "authorize", "acquire", "call", "read_back", "verify", "confirm"})
REASONS = frozenset({
    "remote_tool_error", "protocol_error", "transport_unconfirmed", "contract_changed",
    "invalid_arguments",
    "invalid_response", "read_back_path_missing", "read_back_mismatch", "desktop_busy",
    "operation_identity_unavailable", "authority_revoked", "invalid_contract",
    "output_schema_conflict",
})


class McpEffectFailure(EffectProviderTransportError):
    """未確認の応答を成功回执と区別して Worker の保存処理へ渡す。"""

    def __init__(
        self, *, retryable: bool, diagnostic: dict[str, Any],
        observations: tuple[EffectEvidenceDraft, ...] = (),
    ) -> None:
        """固定の公開分類を使い、例外本文を保持しない。"""
        super().__init__("mcp_effect_unconfirmed", retryable=retryable)
        self.diagnostic = safe_mcp_diagnostic({
            **diagnostic,
            "local_diagnostic_id": local_diagnostic_id(diagnostic.get("local_diagnostic_id")),
        })
        self.observations = observations
        if (self.diagnostic is not None and self.diagnostic["action_attempted"] is False
            and self.diagnostic["stage"] in {"preflight", "acquire"}):
            self.code = "mcp_request_not_sent"


def safe_mcp_diagnostic(value: Any) -> dict[str, Any] | None:
    """任意の診断キーを拒否し、遠端詳細は有界の脱敏処理を再適用する。"""
    if not isinstance(value, Mapping):
        return None
    required = {
        "stage", "reason", "action_attempted", "call_response_received", "read_back_attempts",
    }
    if (not required <= value.keys()
        or not isinstance(value["stage"], str) or not isinstance(value["reason"], str)
        or value["stage"] not in STAGES or value["reason"] not in REASONS
        or type(value["action_attempted"]) is not bool
        or type(value["call_response_received"]) is not bool
        or type(value["read_back_attempts"]) is not int
        or not 0 <= value["read_back_attempts"] <= 3):
        return None
    result = {key: value[key] for key in required}
    diagnostic_id = value.get("diagnostic_id")
    if isinstance(diagnostic_id, str) and re.fullmatch(r"[0-9a-f]{32}", diagnostic_id):
        result["diagnostic_id"] = diagnostic_id
    local_id = value.get("local_diagnostic_id")
    if isinstance(local_id, str) and re.fullmatch(r"[0-9a-f]{32}", local_id):
        result["local_diagnostic_id"] = local_id
    if isinstance(value.get("remote_detail"), dict):
        result["remote_detail"] = safe_remote_detail(value["remote_detail"])
    index = value.get("check_index")
    if type(index) is int and 0 <= index < 20:
        result["check_index"] = index
    return result


def diagnostic_message(value: Any) -> str:
    """既存 Tool error の message に固定分類・相関 ID と非信頼の診断データを表示する。"""
    item = safe_mcp_diagnostic(value)
    if item is None:
        return ""
    message = f" MCP stage={item['stage']}; reason={item['reason']}."
    if "check_index" in item:
        message += f" Read-back check {item['check_index'] + 1}."
    if "diagnostic_id" in item:
        message += f" Diagnostic ID: {item['diagnostic_id']}."
    if "local_diagnostic_id" in item:
        message += f" SKM diagnostic ID: {item['local_diagnostic_id']}."
    if item["action_attempted"]:
        message += " Do not repeat the action; reconcile its original result."
    return message + remote_detail_message(item.get("remote_detail"))
