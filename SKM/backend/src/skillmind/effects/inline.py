"""直接交付の identity と結果を表す内部型。モデル入力から実行権を生成しない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


class InlineUnavailable(Exception):
    """送信前に通常の承認待ち path を選択する。"""


@dataclass(frozen=True, slots=True)
class InlineEffectResult:
    """同じ SDK 要求へ返せる原回执、または延期すべき原提案。"""

    proposal_id: UUID
    receipt: dict[str, Any] | None = None


def inline_success(receipt: dict[str, Any]) -> dict[str, Any]:
    """元 Effect の検証済み回执を公開 Tool の直接交付形へ写す。"""
    from skillmind.effects.continuation import validated_effect_result

    value = validated_effect_result(receipt)
    return {
        "status": "success",
        "deferred": False,
        "delivery": "INLINE",
        "proposal_ref": value["proposal_ref"],
        "outcome": "APPLIED",
        "effect_result": value,
        "evidence_refs": list(
            dict.fromkeys([value[key] for key in ("before_ref", "after_ref") if key in value])
        ),
    }
