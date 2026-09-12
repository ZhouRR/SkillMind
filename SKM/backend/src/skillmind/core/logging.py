"""API と Worker で共通利用する secret-safe JSON logging を提供する。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

_CONTEXT_FIELDS = frozenset({
    "trace_id",
    "request_id",
    "run_id",
    "run_attempt_id",
    "agent_session_id",
    "attempt_no",
    "segment_no",
    "interaction_id",
    "proposal_id",
    "effect_execution_id",
    "worker_id",
    # 解釈の原要求、確定結果と静的 Provider 診断を本文なしで対応付ける。
    "execution_key",
    "skill_source_id",
    "interpretation_id",
    "parent_interpretation_id",
    "attempt",
    "provider_error_kind",
    "provider_result_subtype",
    "provider_api_error_status",
    "provider_exit_code",
    # Agent へ渡した指示の監査値。Brief 正文は Skill guidance と業務入力を含むため、
    # 出力するのは checksum と profile 名だけに留める。
    "task_brief_checksum",
    "execution_profile",
    "status",
    "error_code",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "selected",
    "published",
    "failed",
    "recovered",
    "recovered_runs",
    "recovered_effects",
    "recovered_proposals",
    "recovered_interactions",
})


class JsonLogFormatter(logging.Formatter):
    """一行 JSON と allowlist 済み correlation field だけを出力する。"""

    def format(self, record: logging.LogRecord) -> str:
        """LogRecord を安定 key の JSON object へ変換する。"""

        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "skillmind_event", record.getMessage()),
        }
        # 外部 transport の診断は URL・Session ID・本文を含み得る。level/name だけを残す。
        if record.name.split(".", 1)[0] in {"mcp", "httpx", "httpcore"}:
            payload["event"] = "external_transport.diagnostic"
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        context = getattr(record, "skillmind_context", {})
        if isinstance(context, dict):
            payload.update(context)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    """Process 全体を stdout 向け secret-safe JSON logging に設定する。"""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **context: object,
) -> None:
    """許可済み scalar correlation field だけを structured event に追加する。"""

    safe_context = {
        key: _normalize_scalar(value)
        for key, value in context.items()
        if key in _CONTEXT_FIELDS and value is not None
    }
    logger.log(
        level,
        event,
        extra={"skillmind_event": event, "skillmind_context": safe_context},
    )


def _normalize_scalar(value: object) -> str | int | float | bool:
    """Nested payload を拒否し、UUID と scalar だけを JSON 化可能にする。"""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str | int | float | bool):
        return value
    return type(value).__name__
