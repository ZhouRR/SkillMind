"""API、service と純投影が共有する、原 hash 付きの合成 source fixture。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.skills.runtime_defaults import normalize_runtime_manifest
from skillmind.skills.task_contract import compile_task_contract
from skillmind.skills.task_flow_preview import TaskFlowPreviewSource


def _hash(value: Any) -> str:
    """新規 JSON の原 hash を共通入口で計算する。"""

    return f"sha256:{sha256_hex(canonical_json(value))}"


def _index_hash(value: Any) -> str:
    """保存済み index だけは importer の旧 ASCII escape を保持する。"""

    return "sha256:" + sha256_hex(json.dumps(value, sort_keys=True, separators=(",", ":")))


def make_task_flow_source() -> TaskFlowPreviewSource:
    """別 Task、共有資源、必須規則と確認点を持つ合成済み immutable source を作る。"""

    content = "# 合成 Skill\r\n規則を読む。\r\n結果を説明する。\r\n"
    source_index = [
        {
            "path": "説明/SKILL.md",
            "mime": "text/markdown",
            "size": len(content.encode()),
            "sha256": "sha256:" + sha256_hex(content),
            "binary": False,
        }
    ]
    interpretation_id = uuid4()
    identity = {
        "skill_key": "reading-guide",
        "source_hash": _index_hash(source_index),
        "interpretation_id": str(interpretation_id),
        "interpreter_version": "skillmind-skill-interpreter/2.3.0",
    }
    blueprint: dict[str, Any] = {
        "blueprint_version": "skillmind.capability-blueprint/v1",
        "identity": deepcopy(identity),
        "compatibility": {"level": "adapted"},
        "capabilities": [{"key": "reading.explain", "title": "Read and explain"}],
        "tasks": [
            {"key": "other", "capability": "reading.explain", "objective": "Other objective"},
            {
                "key": "explain",
                "capability": "reading.explain",
                "objective": "Explain input",
                "resource_keys": ["notes", "source"],
                "success_criteria": [{"key": "complete", "text": "Explain the evidence"}],
                "deliverables": [{"key": "report", "kind": "report", "description": "A report"}],
            },
        ],
        "resource_requirements": [
            {"key": "source", "kind": "document", "required": True, "access": "read"},
            {"key": "notes", "kind": "document", "required": False, "access": "read"},
            {"key": "tracker", "kind": "issue", "required": False, "access": "write"},
        ],
        "guidance": {
            "required_rules": [{"key": "cite", "text": "Cite original evidence"}],
            "recommended_steps": [{"key": "read", "text": "Read source first"}],
            "quality_criteria": [{"key": "clear", "text": "Use clear language"}],
            "prohibited_actions": [{"key": "fabricate", "text": "Do not invent evidence"}],
        },
        "interaction_points": [{"key": "review", "type": "REVIEW", "condition": "When requested"}],
        "effect_intents": [
            {
                "key": "update",
                "mode": "apply",
                "resource_key": "tracker",
                "operation": "Record an approved summary",
                "risk": "low",
                "approval_mode": "ask",
            }
        ],
        "execution_preferences": {"recommended_profile": "SUPERVISED"},
        "assumptions": [{"key": "scope", "text": "The input is in scope"}],
        "questions": [{"key": "language", "text": "Which language?", "required": False}],
        "source_traces": [
            {"target": "/tasks/1", "path": "説明/SKILL.md", "line": 1, "reason": "Declared task"},
            {
                "target": "/guidance/required_rules/0",
                "path": "説明/SKILL.md",
                "line": 2,
                "reason": "Declared rule",
            },
            {"target": "/tasks/0", "path": "説明/SKILL.md", "line": None, "reason": "Other task"},
        ],
    }
    manifest: dict[str, Any] = {
        "manifest_version": "skillmind/v1alpha1",
        "identity": identity,
        "compatibility": {"level": "adapted"},
        "tasks": [
            {"key": "other", "capability": "reading.explain"},
            {"key": "explain", "capability": "reading.explain"},
        ],
        "capability_blueprint": blueprint,
    }
    draft = {
        "contract_version": "skillmind.task-contract-draft/v1",
        "type": "object",
        "fields": [],
    }
    compiled = compile_task_contract(draft)
    for task in manifest["tasks"]:
        task.update(
            {
                "input_contract": deepcopy(draft),
                "input_schema": deepcopy(compiled.schema),
                "input_schema_checksum": compiled.checksum,
                "contract_source_trace": [
                    {
                        "contract": "input",
                        "field_path": "/",
                        "source_path": "説明/SKILL.md",
                        "source_section": "Inputs",
                        "line": 1,
                    }
                ],
            }
        )
    manifest = normalize_runtime_manifest(manifest)
    return TaskFlowPreviewSource(
        project_id=uuid4(),
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="reading-guide",
        version="1.2.3",
        manifest_checksum=_hash(manifest),
        manifest=manifest,
        skill_source_id=uuid4(),
        source_hash=identity["source_hash"],
        source_file_index=source_index,
        source_snapshot=[{"path": "説明/SKILL.md", "content": content}],
        interpretation_id=interpretation_id,
        interpreter_version=identity["interpreter_version"],
    )
