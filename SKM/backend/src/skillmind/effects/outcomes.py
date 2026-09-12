"""実行停止と遠端の未実行を分離し、元 Effect の未確定事実を保持する。"""

from __future__ import annotations

from typing import Any

from skillmind.effects.catalog import resolve_effect_capability

UNKNOWN_EFFECT_CODE = "effect_result_unknown"


def effect_failure_record(
    *,
    capability: str,
    attempt_no: int,
    code: str,
    retryable: bool,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """claim 済みの保存処理は原回执で成功を証明するまで、失敗理由だけで未実行としない。"""

    if not resolve_effect_capability(capability).staged_authorization or attempt_no == 0:
        return {"code": code, "retryable": retryable}
    # 呼出し直前で停止した場合も、現在の記録だけでは未送信を証明できない。
    # 生の例外や過去 JSON は複製せず、元の分類 code だけを一定サイズで保持する。
    cause = code
    if previous is not None:
        cause = previous.get(
            "cause_code" if effect_requires_reconciliation(previous) else "code", code
        )
    if not isinstance(cause, str) or not cause.isascii() or not cause.replace("_", "").isalnum():
        cause = "effect_provider_failed"
    return {
        "code": UNKNOWN_EFFECT_CODE,
        "retryable": retryable,
        "cause_code": cause[:80],
        "reason_code": code,
    }


def effect_requires_reconciliation(error: dict[str, Any] | None) -> bool:
    """remote 未確定の印を読み、FAILED/STALE や retryable から実行事実を推定しない。"""

    return error is not None and error.get("code") == UNKNOWN_EFFECT_CODE
