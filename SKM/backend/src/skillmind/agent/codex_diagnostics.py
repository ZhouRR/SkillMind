"""Codex の失敗から固定分類と HTTP status だけを抽出する。"""

from __future__ import annotations

import json
from collections.abc import Mapping

_CODES = frozenset({
    "invalid_json_schema", "invalid_request_error", "authentication_error",
    "invalid_api_key", "rate_limit_exceeded", "insufficient_quota",
    "server_error", "context_length_exceeded", "model_not_found", "server_overloaded",
})


def codex_failure_detail(error: object) -> str:
    """本文・URL・header を転記せず、許可した分類だけを診断へ残す。"""

    code = "unknown"
    status: int | None = None
    current = error
    for _ in range(5):
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except (ValueError, RecursionError):
                break
        if not isinstance(current, Mapping):
            break
        for key in ("code", "type", "codexErrorInfo", "codex_error_info"):
            value = current.get(key)
            if value == "serverOverloaded":
                value = "server_overloaded"
            if isinstance(value, str) and value in _CODES:
                code = value
                break
        value = current.get("status")
        if type(value) is int and 400 <= value <= 599:
            status = value
        current = current.get("error", current.get("message"))
    return f"codex:{code}" + (f"; http_status={status}" if status is not None else "")
