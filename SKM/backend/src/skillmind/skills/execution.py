"""原文実行の資源選択と platform 所有の Tool 方針を分離する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

EXECUTION_VERSION = "skillmind.skill-execution/v2"
PLATFORM_TOOLS = frozenset(
    {
        "workspace.read/v1",
        "workspace.image/v1",
        "workspace.search/v1",
        "workspace.write/v1",
        "workspace.write/v2",
        "workspace.edit/v1",
        "document.files/v1",
        "audit.export/v1",
        "artifact.append/v1",
        "artifact.materialize/v1",
        "tool.sequence/v1",
        "json.schema.validate/v1",
        "subagent.dispatch/v1",
        "interaction.request/v1",
    }
)


# Tool は model に選別させない。外部操作と子 Agent は資源/配備の別権限で解決する。
DEFAULT_TOOLS = PLATFORM_TOOLS - {
    "workspace.write/v1", "document.files/v1", "subagent.dispatch/v1",
}


def is_source_execution(definition: Mapping[str, Any]) -> bool:
    """実行方式は version で識別し、欠損を新方式として扱わない。"""

    return definition.get("execution_version") == EXECUTION_VERSION


def resolve_skill_definition(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """保存された宣言をそのまま返す。旧 Manifest から Blueprint を逆生成しない。"""

    if "skill_execution" in manifest:
        value = manifest["skill_execution"]
        if "capability_blueprint" in manifest or not isinstance(value, dict):
            raise ValueError("Conflicting or invalid Skill execution declaration")
        if not is_source_execution(value):
            raise ValueError("Unsupported Skill execution version")
        return deepcopy(value)
    value = manifest.get("capability_blueprint")
    return deepcopy(value) if isinstance(value, dict) else None


def declared_operations(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Blueprint の apply 宣言だけを読む。原文実行の操作許可は Run 資源に属する。"""

    if not is_source_execution(definition):
        return [
            dict(item)
            for item in definition.get("effect_intents", [])
            if isinstance(item, Mapping) and item.get("mode") == "apply"
        ]
    return []


def validate_execution(definition: Mapping[str, Any], contracts_dir: Path) -> None:
    """資源選択の形を検査し、Tool や操作許可を model に生成させない。"""

    from skillmind.skills.capability_blueprint import CapabilityBlueprintError

    schema = json.loads((contracts_dir / "skill-execution/v2.schema.json").read_text())
    error = next(Draft202012Validator(schema).iter_errors(definition), None)
    if error is not None:
        path = "/skill_execution/" + "/".join(str(part) for part in error.absolute_path)
        raise CapabilityBlueprintError(
            "skill_execution_invalid", path,
            "Skill execution declaration does not match its schema."
        )
    resources = definition["resource_requirements"]
    keys = [r["key"] for r in resources]
    if len(set(keys)) != len(keys) or definition["tasks"][0]["resource_keys"] != keys:
        raise CapabilityBlueprintError(
            "skill_execution_invalid",
            "/skill_execution",
            "Resource keys must be unique and match the task.",
        )
    for i, resource in enumerate(resources):
        if resource["selection"] == "documents" and resource["kind"] != "document":
            raise CapabilityBlueprintError(
                "skill_execution_invalid", f"/skill_execution/resource_requirements/{i}",
                "Document selection requires a document resource.",
            )
