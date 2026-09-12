"""文書取得前に必要な確定済み effect intent を、凍結 Task 宣言から解決する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DOCUMENT_READINESS_CAPABILITY = "document.readiness/v1"


@dataclass(frozen=True, slots=True)
class DocumentPrerequisite:
    """前置効果の原 intent・write slot・operation を保持し、権限には昇格させない。"""

    intent_key: str
    resource_key: str
    operation: str


def document_prerequisites(
    manifest: Mapping[str, Any], task_key: str, *, check_runtime_capability: bool = True
) -> tuple[DocumentPrerequisite, ...]:
    """旧 Task の省略は維持し、宣言された gate の欠落参照や未対応 Worker を拒否する。"""

    blueprint = manifest.get("capability_blueprint")
    if not isinstance(blueprint, Mapping):
        return ()
    tasks = blueprint.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("Document prerequisite tasks are invalid")
    task = next(
        (item for item in tasks if isinstance(item, Mapping) and item.get("key") == task_key), None
    )
    if task is None and any(
        isinstance(item, Mapping) and "document_prerequisites" in item for item in tasks
    ):
        raise ValueError("Document prerequisite task is unavailable")
    if task is None or "document_prerequisites" not in task:
        return ()
    keys = task["document_prerequisites"]
    if (
        not isinstance(keys, list)
        or not 1 <= len(keys) <= 50
        or any(not isinstance(key, str) or not key for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise ValueError("Document prerequisites are invalid")
    # 旧 Worker は required な新能力を解決できず、gate を無視した実行へ進めない。
    tools = manifest.get("tools", [])
    if check_runtime_capability and (
        not isinstance(tools, list)
        or not any(
            isinstance(tool, Mapping)
            and tool.get("capability") == DOCUMENT_READINESS_CAPABILITY
            and tool.get("required") is True
            for tool in tools
        )
    ):
        raise ValueError("Document prerequisites require the readiness capability")
    intents = blueprint.get("effect_intents", [])
    resources = blueprint.get("resource_requirements", [])
    if not isinstance(intents, list) or not isinstance(resources, list):
        raise ValueError("Document prerequisite declarations are invalid")
    result = []
    for key in keys:
        matches = [item for item in intents if isinstance(item, Mapping) and item.get("key") == key]
        if len(matches) != 1:
            raise ValueError("Document prerequisite intent is unavailable")
        intent = matches[0]
        resource_key, operation = intent.get("resource_key"), intent.get("operation")
        if (
            intent.get("mode") != "apply"
            or intent.get("approval_mode") != "ask"
            or not isinstance(resource_key, str)
            or not isinstance(operation, str)
            or not any(
                isinstance(item, Mapping)
                and item.get("key") == resource_key
                and item.get("access") == "write"
                for item in resources
            )
        ):
            raise ValueError("Document prerequisite must identify an approved write intent")
        result.append(DocumentPrerequisite(key, resource_key, operation))
    return tuple(result)
