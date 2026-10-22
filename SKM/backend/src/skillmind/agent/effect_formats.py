"""Run が実際に許可した外部書込能力の提案形式を、契約の説明から取り出す。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skillmind.agent.contract_store import ContractStore


def effect_formats(contracts: ContractStore, effect_policy: Mapping[str, Any]) -> dict[str, str]:
    """許可済み operation の capability ごとに、契約が定める提案 payload の説明を返す。

    説明は権限ではなく、契約の `description` をそのまま使う。Tool の説明は Claude CLI が
    2048 文字で切るため、Run ごとに必要な能力だけを task brief で確実に届ける。
    """

    # Effect 側も Agent の Provider を読むため、起動時の循環 import を避ける。
    from skillmind.effects.catalog import EFFECT_CAPABILITIES

    operations = effect_policy.get("operations", [])
    capabilities = {
        item["capability_version"]
        for item in operations
        if isinstance(item, Mapping)
        and isinstance(item.get("capability_version"), str)
        and item["capability_version"] in EFFECT_CAPABILITIES
    }
    return {
        capability: str(contracts.load(f"tools/{capability}/request.schema.json")["description"])
        for capability in sorted(capabilities)
    }
