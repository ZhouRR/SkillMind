"""入力・資源だけの候補を原文実行 Manifest へ編訳する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.skills.candidate import _omit_optional_nulls, source_index
from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.execution import EXECUTION_VERSION, PLATFORM_TOOLS, validate_execution
from skillmind.skills.importer import _skill_key
from skillmind.skills.source_documents import validate_source_location

CANDIDATE_VERSION = "skillmind.skill-candidate/v2"
CANDIDATE_SCHEMA_ID = "https://schemas.skillmind.local/skills/interpreter/v2/candidate.schema.json"


def candidate_schema(contracts_dir: Path) -> dict[str, Any]:
    """新規解釈で使う小さい native Schema をロードする。"""

    schema: dict[str, Any] = json.loads(
        (contracts_dir / "skills/interpreter/v2/candidate.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    return schema


def compile_candidate(
    request: Mapping[str, Any], raw: Mapping[str, Any], root: Path
) -> dict[str, Any]:
    """原文を複写させず、task 一件と binding 要求だけを確定する。"""

    # Run API は object の入力だけを受け付ける。汎用契約の nested 型や旧版は狭めない。
    input_contract = raw.get("input_contract")
    if isinstance(input_contract, Mapping) and input_contract.get("type") != "object":
        raise CapabilityBlueprintError(
            "candidate_input_root_invalid",
            "/input_contract/type",
            "Caller input must have an object root; nested arrays and scalar fields are allowed.",
        )
    Draft202012Validator(candidate_schema(root)).validate(raw)
    sources = source_index(request)
    files = {s["path"]: s["content"] for s in sources}

    def location(reference: str | None) -> dict[str, Any]:
        """番号付き原文の厳密な位置だけを受け付ける。"""

        if reference is None:
            return {"path": None, "line": None}
        source_id, _, line = reference.partition(":")
        source = next((s for s in sources if s["id"] == source_id), None)
        if source is None:
            raise CapabilityBlueprintError(
                "candidate_source_invalid", "/source_ref", "Use a frozen source ID."
            )
        value = {"path": source["path"], "line": int(line) if line else None}
        validate_source_location(value["path"], value["line"], files, pointer="/source_ref")
        return value

    value = _omit_optional_nulls(deepcopy(dict(raw)))
    source, identity = request["source"], request["interpreter"]
    metadata = source["normalized_package"]["metadata"]
    title = value.get("title") or metadata["name"]
    description = value.get("description") or metadata.get("description") or title
    skill_key = _skill_key(metadata["name"], source["content_hash"])
    resources = value["resource_requirements"]
    traces = []
    for i, resource in enumerate(resources):
        traces.append(
            {"target": f"/resource_requirements/{i}", **location(resource.pop("source_ref"))}
        )
    task = {
        "key": "execute",
        "capability": skill_key,
        "title": title,
        "description": description,
        "resource_keys": [r["key"] for r in resources],
    }
    definition = {
        "execution_version": EXECUTION_VERSION,
        "tasks": [task],
        "resource_requirements": resources,
        "source_traces": traces,
    }
    validate_execution(definition, root)
    platform_tools = value["platform_tools"]
    if len(set(platform_tools)) != len(platform_tools) or not set(platform_tools) <= PLATFORM_TOOLS:
        raise CapabilityBlueprintError(
            "skill_execution_invalid",
            "/platform_tools",
            "Choose only supported task-local platform Tools.",
        )
    # 外部 Tools は宣言済み resource から導出し、binding と二重宣言させない。
    from skillmind.effects.catalog import EFFECT_CAPABILITIES

    tools = set(platform_tools) | {"interaction.request/v1", "workspace.read/v1"}
    required_tools = set(tools)
    for resource in resources:
        if resource["required"]:
            required_tools.update(resource["capabilities"])
        tools.update(cap for cap in resource["capabilities"] if cap not in EFFECT_CAPABILITIES)
        if resource["operations"]:
            tools.add("change.propose/v1")
            required_tools.add("change.propose/v1")
    diagnostics = [
        {
            "severity": d["severity"],
            "code": d["code"],
            "message": d["message"],
            **location(d.get("source_ref")),
        }
        for d in value["diagnostics"]
    ]
    level = "assisted" if any(d["severity"] == "error" for d in diagnostics) else "adapted"
    input_location = location(value["input_source_ref"])
    manifest = {
        "manifest_version": "skillmind/v1alpha1",
        "identity": {
            "skill_key": skill_key,
            "source_hash": source["content_hash"],
            "interpretation_id": "00000000-0000-4000-8000-000000000001",
            "interpreter_version": f"{identity['skill_key']}/{identity['version']}",
        },
        "compatibility": {"level": level, "diagnostics": diagnostics},
        "skill_execution": definition,
        "capabilities": [{"key": skill_key, "title": title}],
        "tasks": [
            {
                "key": "execute",
                "capability": skill_key,
                "input_contract": value["input_contract"],
                "contract_source_trace": [
                    {
                        "contract": "input",
                        "field_path": "/",
                        "source_path": input_location["path"],
                        "line": input_location["line"],
                    }
                ],
            }
        ],
        "tools": [{"capability": t, "required": t in required_tools} for t in sorted(tools)],
    }
    return {
        "response_version": "skillmind.skill-interpreter.response/v1",
        "source_hash": source["content_hash"],
        "interpreter": {k: identity[k] for k in ("skill_key", "version", "prompt_checksum")},
        "runtime_manifest_draft": manifest,
        "report": {
            "report_version": "skillmind.skill-interpretation-report/v1",
            "summary": description,
            "compatibility_level": level,
            "diagnostics": diagnostics,
            "source_traces": [
                {
                    "target": "/tasks/0/input_contract",
                    **input_location,
                    "reason": "Input declaration references the frozen source.",
                }
            ],
        },
    }
