"""監査済みの前段状態とモデルの追加 checkpoint を、原候補を変えずに合成する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from skillmind.core.hashing import canonical_json


def merge_checkpoint(previous: Mapping[str, Any], additions: Mapping[str, Any]) -> dict[str, Any]:
    """事実と参照を累積し、summary は最新値、effect_result は現回の回执だけにする。"""

    checkpoint = deepcopy(dict(additions))
    checkpoint.pop("effect_result", None)
    for key in (
        "confirmed_facts", "evidence_refs", "artifact_refs", "change_proposal_refs",
        "user_responses",
    ):
        if key not in previous and key not in additions:
            continue
        values = [*previous.get(key, []), *additions.get(key, [])]
        # 全量を送る旧 client も重複させず、原値と安定順序を保持する。
        unique = {canonical_json(value): deepcopy(value) for value in values}
        checkpoint[key] = list(unique.values())
    return checkpoint
