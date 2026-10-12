"""入力・資源だけの候補を原文実行 Manifest へ編訳する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.execution import EXECUTION_VERSION, validate_execution
from skillmind.skills.importer import _skill_key
from skillmind.skills.source_projection import (
    SourceLocations,
    source_index,
)
from skillmind.skills.source_projection import (
    omit_optional_nulls as _omit_optional_nulls,
)

CANDIDATE_VERSION = "skillmind.skill-candidate/v3"
CANDIDATE_SCHEMA_ID = "https://schemas.skillmind.local/skills/interpreter/v3/candidate.schema.json"


def candidate_schema(contracts_dir: Path) -> dict[str, Any]:
    """新規解釈で使う小さい native Schema をロードする。"""

    schema: dict[str, Any] = json.loads(
        (contracts_dir / "skills/interpreter/v3/candidate.schema.json").read_text(encoding="utf-8")
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
    location = SourceLocations(source_index(request)).resolve

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
            {
                "target": f"/resource_requirements/{i}",
                **location(resource.pop("source_ref"), f"/resource_requirements/{i}/source_ref"),
            }
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
    diagnostics = [
        {
            "severity": d["severity"],
            "code": d["code"],
            "message": d["message"],
            **location(d.get("source_ref"), f"/diagnostics/{i}/source_ref"),
        }
        for i, d in enumerate(value["diagnostics"])
    ]
    level = "assisted" if any(d["severity"] == "error" for d in diagnostics) else "adapted"
    input_location = location(value["input_source_ref"], "/input_source_ref")
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
        # 原文は Tool の許可リストを持たない。Run が実資源と配備から解決する。
        "tools": [],
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
