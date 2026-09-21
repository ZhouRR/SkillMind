"""原文実行の最小宣言と旧 Blueprint を、意味を補作せず共有 consumer へ渡す。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.documents.library_contract import has_invalid_document_library_capabilities
from skillmind.effects.operation_policy import WRITE_OPERATIONS

EXECUTION_VERSION = "skillmind.skill-execution/v1"
PLATFORM_TOOLS = frozenset(
    {
        "workspace.read/v1",
        "workspace.search/v1",
        "workspace.write/v1",
        "workspace.write/v2",
        "audit.export/v1",
        "tool.sequence/v1",
        "json.schema.validate/v1",
        "subagent.dispatch/v1",
        "interaction.request/v1",
    }
)


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
    """新方式は資源別の許可候補を、旧方式は原 apply 宣言を読み取る。"""

    if not is_source_execution(definition):
        return [
            dict(item)
            for item in definition.get("effect_intents", [])
            if isinstance(item, Mapping) and item.get("mode") == "apply"
        ]
    return [
        {"resource_key": resource["key"], **operation}
        for resource in definition.get("resource_requirements", [])
        for operation in resource.get("operations", [])
    ]


def validate_execution(definition: Mapping[str, Any], contracts_dir: Path) -> None:
    """最小宣言の参照、操作と読書分離を検査し、業務手順を再解釈しない。"""

    from skillmind.skills.capability_blueprint import CapabilityBlueprintError

    schema = json.loads((contracts_dir / "skill-execution/v1.schema.json").read_text())
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
    catalog = json.loads((contracts_dir / "examples/skill-capability-catalog.v1.json").read_text())
    available = {c["capability"] for c in catalog["capabilities"]} - PLATFORM_TOOLS - {
        "change.propose/v1", "document.readiness/v1",
    }
    for i, resource in enumerate(resources):
        path = f"/skill_execution/resource_requirements/{i}"
        caps = resource.get("capabilities", [])
        if not caps or len(set(caps)) != len(caps) or not set(caps) <= available:
            raise CapabilityBlueprintError(
                "skill_execution_invalid",
                path,
                "Resource capabilities must be registered and unique.",
            )
        providers = {provider for entry in catalog["capabilities"]
                     if entry["capability"] in caps for provider in entry["providers"]}
        accepted = resource.get("accepted_providers", [])
        if len(set(accepted)) != len(accepted) or not set(accepted) <= providers:
            raise CapabilityBlueprintError(
                "skill_execution_invalid", path,
                "Provider hints must match the registered resource capabilities.",
            )
        if has_invalid_document_library_capabilities(resource):
            raise CapabilityBlueprintError(
                "document_library_capabilities_invalid",
                path,
                "Artifact library destinations allow document.write/v1 only.",
            )
        operations = resource["operations"]
        pairs = {(x["capability_version"], x["operation"]) for x in operations}
        if len(pairs) != len(operations) or any(
            cap not in caps or op not in WRITE_OPERATIONS.get(cap, ()) for cap, op in pairs
        ):
            raise CapabilityBlueprintError(
                "skill_execution_invalid",
                path,
                "Use registered operations on the declared resource.",
            )
        writes = set(caps) & WRITE_OPERATIONS.keys()
        if (
            writes != {cap for cap, _ in pairs}
            or (writes and (resource["access"] != "write" or resource["required"] is not True))
            or (not writes and resource["access"] != "read")
        ):
            raise CapabilityBlueprintError(
                "skill_execution_invalid",
                path,
                "Writes need an explicit operation and required write resource.",
            )
