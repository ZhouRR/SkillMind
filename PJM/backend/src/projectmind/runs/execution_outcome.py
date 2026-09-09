"""実行停止を platform の監査事実へ変換する共通規則。"""

from __future__ import annotations

from dataclasses import replace

from projectmind.agent.domain import AgentEvent, AgentEventType


def user_cancellation_event(event: AgentEvent, *, sequence: int | None = None) -> AgentEvent:
    """確認済み取消の event を作り、観測用量を失わず結果本文を持ち越さない。

    この関数は取消を授権しない。呼出し側は持久 intent を検証し、repository は
    Run lock 下でも再検証する。SDK の reason 文字列だけを根拠に呼ばない。
    """

    observed = {
        key: event.payload[key]
        for key in (
            "subtype",
            "stop_reason",
            "num_turns",
            "duration_ms",
            "duration_api_ms",
            "total_cost_usd",
            "usage",
        )
        if key in event.payload
    }
    return replace(
        event,
        sequence=event.sequence if sequence is None else sequence,
        event_type=AgentEventType.SESSION_INTERRUPTED,
        payload={**observed, "reason": "user_interrupted"},
    )
