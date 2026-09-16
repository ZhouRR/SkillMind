"""単一の model Blueprint 候補を、既存の不変な公開形式へ決定的に編訳する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.importer import _skill_key
from skillmind.skills.source_documents import validate_source_documents, validate_source_location

CANDIDATE_VERSION = "skillmind.skill-candidate/v1"
CANDIDATE_SCHEMA_ID = (
    "https://schemas.skillmind.local/skills/interpreter/v1/candidate.schema.json"
)


def candidate_schema(contracts_dir: Path) -> dict[str, Any]:
    """API/Worker と契約 test が同じ native candidate Schema を読む。"""

    schema: dict[str, Any] = json.loads(
        (contracts_dir / "skills/interpreter/v1/candidate.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    return schema


def source_index(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """凍結 index 順の識別子を原 path と全文へ対応付け、binary は本文無しで示す。"""

    source = request["source"]
    documents = validate_source_documents(source["source_documents"])
    contents = {item["path"]: item["content"] for item in documents}
    expected = {item["path"]: (item["sha256"], item["size"])
                for item in source["normalized_package"]["source"]["files"] if not item["binary"]}
    actual = {item["path"]: (item["sha256"], len(item["content"].encode("utf-8")))
              for item in documents}
    if expected != actual:
        raise ValueError("Frozen Skill source documents differ from the source index")
    return [
        {"id": f"s{index}", "path": item["path"], "content": contents.get(item["path"])}
        for index, item in enumerate(source["normalized_package"]["source"]["files"])
    ]


def model_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """原文を一度だけ番号付きで提示し、保存 request 自体は変更しない。"""

    sources = []
    for source in source_index(request):
        content = source["content"]
        sources.append({
            "id": source["id"], "path": source["path"],
            "numbered_text": None if content is None else "".join(
                f"{number}: {line}" for number, line in enumerate(
                    content.splitlines(keepends=True), start=1
                )
            ),
        })
    return {
        "sources": sources,
        "capabilities": [
            {"capability": item["capability"], "description": item["description"],
             "providers": item["providers"]}
            for item in request["capability_catalog"]["capabilities"]
        ],
        "static_analysis": deepcopy(request["static_analysis"]),
        "previous_interpretation": deepcopy(request.get("previous_interpretation")),
        "adjustment": deepcopy(request.get("adjustment")),
    }


def _omit_optional_nulls(value: Any) -> Any:
    """候補の optional null を省略へ戻す。配列内の業務値は削除しない。"""

    if isinstance(value, dict):
        return {key: _omit_optional_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_optional_nulls(item) for item in value]
    return value


def compile_candidate(
    request: Mapping[str, Any], candidate: Mapping[str, Any], schema: Mapping[str, Any],
) -> dict[str, Any]:
    """意味宣言は model のまま保ち、重複 Manifest/Report と source 位置だけを生成する。"""

    Draft202012Validator(schema).validate(candidate)
    sources = source_index(request)
    files = {item["path"]: item["content"] for item in sources}

    def location(reference: str | None, pointer: str) -> dict[str, Any]:
        """明示された source ID/行を厳密に解決し、越境や推測補修を拒否する。"""

        if reference is None:
            return {"path": None, "line": None}
        source_id, _, line_text = reference.partition(":")
        source = next((item for item in sources if item["id"] == source_id), None)
        if source is None:
            raise CapabilityBlueprintError(
                "candidate_source_invalid", pointer, "Use an ID from the frozen sources list."
            )
        line = int(line_text) if line_text else None
        validate_source_location(source["path"], line, files, pointer=pointer)
        return {"path": source["path"], "line": line}

    value = _omit_optional_nulls(deepcopy(dict(candidate)))
    blueprint = {key: item for key, item in value.items() if key not in {
        "candidate_version", "summary", "compatibility_level", "diagnostics", "tools",
    }}
    blueprint["blueprint_version"] = "skillmind.capability-blueprint/v1"
    for index, trace in enumerate(blueprint["source_traces"]):
        trace.update(location(trace.pop("source_ref"), f"/source_traces/{index}/source_ref"))
    report_traces = deepcopy(blueprint["source_traces"])
    tools = []
    for index, tool in enumerate(value["tools"]):
        tools.append({"capability": tool["capability"], "required": tool["required"]})
        report_traces.append({
            "target": f"/tools/{index}", "reason": tool["reason"],
            **location(tool["source_ref"], f"/tools/{index}/source_ref"),
        })
    diagnostics = [
        {"severity": item["severity"], "code": item["code"], "message": item["message"],
         **location(item.get("source_ref"), f"/diagnostics/{index}/source_ref")}
        for index, item in enumerate(value["diagnostics"])
    ]
    tasks, workflows = [], []
    for index, task in enumerate(blueprint["tasks"]):
        trace_entries: list[dict[str, Any]] = []
        for kind, name in (("input", "parameter_contract"), ("output", "result_contract")):
            if name not in task:
                continue
            prefix = f"/tasks/{index}/{name}"
            contract_traces = [t for t in blueprint["source_traces"] if (
                t["target"] == prefix or t["target"].startswith(prefix + "/")
            )]
            if not contract_traces:
                raise CapabilityBlueprintError(
                    "candidate_contract_source_missing", prefix,
                    "Trace each declared task contract to the frozen source; "
                    "an empty input object still needs task-purpose evidence.",
                )
            trace_entries.extend({
                "contract": kind, "field_path": t["target"][len(prefix):] or "/",
                "source_path": t["path"], "line": t["line"],
            } for t in contract_traces)
        workflow_key = task["key"]
        runtime_task = {
            "key": task["key"], "capability": task["capability"],
            "input_contract": deepcopy(task["parameter_contract"]),
            "contract_source_trace": trace_entries, "workflow": workflow_key,
        }
        if "result_contract" in task:
            runtime_task["output_contract"] = deepcopy(task["result_contract"])
        tasks.append(runtime_task)
        workflows.append({"key": workflow_key, "steps": [
            *[{"key": f"tool-{n}", "kind": "tool"} for n in range(len(tools))],
            {"key": "execute", "kind": "agent"},
            {"key": "validate", "kind": "validator"},
            {"key": "result", "kind": "artifact"},
        ]})
    source = request["source"]
    identity = request["interpreter"]
    manifest = {
        "manifest_version": "skillmind/v1alpha1",
        "identity": {
            "skill_key": _skill_key(source["normalized_package"]["metadata"]["name"],
                                    source["content_hash"]),
            "source_hash": source["content_hash"],
            # 発行時には実 Interpretation UUID へ既存の凍結処理が束縛する。
            "interpretation_id": "00000000-0000-4000-8000-000000000001",
            "interpreter_version": f"{identity['skill_key']}/{identity['version']}",
        },
        "compatibility": {"level": value["compatibility_level"], "diagnostics": diagnostics},
        "capability_blueprint": blueprint,
        "capabilities": [{"key": c["key"], "title": c["title"]}
                         for c in blueprint["capabilities"]],
        "tasks": tasks, "tools": tools, "workflows": workflows,
    }
    return {
        "response_version": "skillmind.skill-interpreter.response/v1",
        "source_hash": source["content_hash"],
        "interpreter": {k: identity[k] for k in ("skill_key", "version", "prompt_checksum")},
        "runtime_manifest_draft": manifest,
        "report": {
            "report_version": "skillmind.skill-interpretation-report/v1",
            "summary": value["summary"], "compatibility_level": value["compatibility_level"],
            "diagnostics": diagnostics, "source_traces": report_traces,
        },
    }
