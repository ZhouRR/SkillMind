"""実行停止と遠端の未実行を分離し、元 Effect の未確定事実を保持する。"""

from __future__ import annotations

import re
from typing import Any

from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.mcp_diagnostics import safe_mcp_diagnostic

UNKNOWN_EFFECT_CODE = "effect_result_unknown"


def effect_failure_record(
    *,
    capability: str,
    attempt_no: int,
    code: str,
    retryable: bool,
    previous: dict[str, Any] | None,
    provider: str | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """claim 済みの保存処理は原回执で成功を証明するまで、失敗理由だけで未実行としない。"""

    if not resolve_effect_capability(capability).supports_supervision(provider) or attempt_no == 0:
        return {"code": code, "retryable": retryable}
    safe = safe_mcp_diagnostic(diagnostic)
    if (capability == "mcp.call/v1" and attempt_no == 1 and code == "mcp_request_not_sent"
        and not effect_requires_reconciliation(previous) and safe is not None
        and safe["action_attempted"] is False and safe["call_response_received"] is False
        and safe["stage"] in {"preflight", "acquire"}):
        return {"code": code, "retryable": False, "diagnostic": safe}
    # 呼出し直前で停止した場合も、現在の記録だけでは未送信を証明できない。
    # 生の例外や過去 JSON は複製せず、元の分類 code だけを一定サイズで保持する。
    cause = code
    if previous is not None:
        cause = previous.get(
            "cause_code" if effect_requires_reconciliation(previous) else "code", code
        )
    if not isinstance(cause, str) or not cause.isascii() or not cause.replace("_", "").isalnum():
        cause = "effect_provider_failed"
    record: dict[str, Any] = {
        "code": UNKNOWN_EFFECT_CODE,
        "retryable": retryable,
        "cause_code": cause[:80],
        "reason_code": code,
    }
    if capability == "mcp.call/v1":
        safe = safe_mcp_diagnostic(diagnostic or (previous or {}).get("diagnostic"))
        if safe is not None:
            record["diagnostic"] = safe
        refs = (previous or {}).get("observation_refs", [])
        if isinstance(refs, list):
            refs = [ref for ref in refs if isinstance(ref, str)
                    and re.fullmatch(r"ev_[a-zA-Z0-9_-]{1,128}", ref)][:16]
            if refs:
                record["observation_refs"] = refs
    return record


def effect_requires_reconciliation(error: dict[str, Any] | None) -> bool:
    """remote 未確定の印を読み、FAILED/STALE や retryable から実行事実を推定しない。"""

    return error is not None and error.get("code") == UNKNOWN_EFFECT_CODE
