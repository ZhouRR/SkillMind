"""凍結 Run の資源操作・リスク上限と提案の意図を照合する。"""

from __future__ import annotations

from typing import Any

from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.operation_policy import operation_authorization_key, operation_risk
from skillmind.skills.execution import is_source_execution


def validate_effect_intent(
    blueprint: dict[str, Any], *, effect_intent_key: str, resource_key: str,
    operation: str, risk_level: str, capability_version: str | None = None,
    resource_operations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """初回提案と段階認可で同じ apply 意図/対象/操作/リスク境界を使う。"""

    if is_source_execution(blueprint):
        matches = [op for op in resource_operations or []
                   if op["resource_key"] == resource_key
                   and op["capability_version"] == capability_version
                   and op["operation"] == operation]
        levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        if len(matches) != 1 or capability_version is None or effect_intent_key != (
            operation_authorization_key(resource_key, capability_version, operation)
        ) or levels.get(risk_level, -1) < levels[operation_risk(capability_version)]:
            raise ChangeProposalValidationError(
                "ChangeProposal does not match an authorized resource operation"
            )
        return matches[0]
    intents = [
        item for item in blueprint.get("effect_intents", [])
        if isinstance(item, dict) and item.get("key") == effect_intent_key
    ]
    if len(intents) != 1 or intents[0].get("mode") != "apply":
        raise ChangeProposalValidationError(
            "ChangeProposal does not match a declared apply effect intent"
        )
    intent = intents[0]
    if intent.get("resource_key") != resource_key:
        raise ChangeProposalValidationError("ChangeProposal targets another resource")
    if intent.get("operation") != operation:
        raise ChangeProposalValidationError("ChangeProposal operation changed from Blueprint")
    if str(intent.get("risk", "")).upper() != risk_level:
        raise ChangeProposalValidationError("ChangeProposal risk changed from Blueprint")
    return intent

