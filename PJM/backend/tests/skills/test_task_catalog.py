"""PUBLISHED Manifest から task descriptor を投影する純関数を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from projectmind.agent.outcome import compile_outcome_schema
from projectmind.skills import (
    PublishedTaskDescriptor,
    project_published_tasks,
    resolve_task_run_from_manifest,
)

PUBLISHED_AT = datetime(2026, 7, 9, tzinfo=UTC)
INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"subject": {"type": "string"}},
    "required": ["subject"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
INPUT_CHECKSUM = "sha256:" + ("b" * 64)
OUTPUT_CHECKSUM = "sha256:" + ("c" * 64)


def _task(key: str, capability: str, workflow: str, view: str) -> dict[str, Any]:
    """Generated Task Contract を持つ task manifest entry を組み立てる。"""

    return {
        "key": key,
        "capability": capability,
        "type": "immediate",
        "input_contract": {
            "contract_version": "projectmind.task-contract-draft/v1",
            "type": "object",
            "fields": [],
        },
        "output_contract": {
            "contract_version": "projectmind.task-contract-draft/v1",
            "type": "object",
            "fields": [],
        },
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "input_schema_checksum": INPUT_CHECKSUM,
        "output_schema_checksum": OUTPUT_CHECKSUM,
        "contract_source_trace": [
            {
                "contract": "input",
                "field_path": "/",
                "source_path": "SKILL.md",
                "source_section": "Inputs",
                "line": 1,
            }
        ],
        "workflow": workflow,
        "view": view,
    }


def _manifest(**overrides: Any) -> dict[str, Any]:
    """非 JAF の汎用 repository-review Manifest を組み立てる。"""

    manifest: dict[str, Any] = {
        "manifest_version": "projectmind/v1alpha1",
        "compatibility": {"level": "adapted", "confidence": 0.72, "diagnostics": []},
        "capabilities": [
            {"key": "repository.review", "title": "Repository Review"},
        ],
        "tasks": [
            _task("review-change", "repository.review", "review-change-v1", "standard")
        ],
        "tools": [{"capability": "repository.read/v1", "required": True}],
        "workflows": [{"key": "review-change-v1", "steps": [{"key": "analyze", "kind": "agent"}]}],
        "ui": {"default_view": "standard", "views": []},
        "tests": [],
        # 発行済み manifest は必ず蓝图を持つ。投影による補完は廃止済みのため fixture が備える。
        "capability_blueprint": _blueprint(),
    }
    manifest.update(overrides)
    return manifest


def _blueprint(**overrides: Any) -> dict[str, Any]:
    """Manifest が宣言する資源を開示した最小の能力蓝图を返す。"""

    blueprint: dict[str, Any] = {
        "blueprint_version": "projectmind.capability-blueprint/v1",
        "identity": {
            "skill_key": "repository-review",
            "source_hash": "sha256:" + ("a" * 64),
            "interpretation_id": "00000000-0000-4000-8000-000000000123",
            "interpreter_version": "projectmind-skill-interpreter/2.3.0",
        },
        "compatibility": {"level": "adapted"},
        "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
        "tasks": [
            {
                "key": "review-change",
                "capability": "repository.review",
                "objective": "Review the selected change and report findings.",
            }
        ],
        "resource_requirements": [
            {
                "key": "repository-source",
                "kind": "repository",
                "required": True,
                "access": "read",
                "capabilities": ["repository.read/v1"],
                "accepted_providers": ["git"],
            }
        ],
        "guidance": {
            "required_rules": [],
            "recommended_steps": [],
            "quality_criteria": [],
            "prohibited_actions": [],
        },
        "source_traces": [
            {
                "target": "/tasks/0",
                "path": "SKILL.md",
                "line": 5,
                "reason": "The Goal section defines the review objective.",
            }
        ],
    }
    blueprint.update(overrides)
    return blueprint


def _project(manifest: dict[str, Any]) -> list[PublishedTaskDescriptor]:
    """固定 identity で投影を実行する。"""

    return project_published_tasks(
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="repository-review",
        skill_name="Repository Review",
        version="1.0.0",
        published_at=PUBLISHED_AT,
        manifest=manifest,
    )


def test_projects_generic_task_with_schemas_workflow_view_and_requirements() -> None:
    """汎用 Manifest の task を精確 version 束縛付き descriptor へ投影する。"""

    tasks = _project(_manifest())

    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_key == "review-change"
    assert task.capability == "repository.review"
    assert task.title == "Repository Review"
    assert task.task_type == "immediate"
    assert task.input_schema == INPUT_SCHEMA
    outcome = compile_outcome_schema(OUTPUT_SCHEMA, task_schema_checksum=OUTPUT_CHECKSUM)
    assert task.output_schema == outcome.schema
    assert task.input_schema_checksum == INPUT_CHECKSUM
    assert task.output_schema_checksum == outcome.checksum
    assert task.task_output_schema == OUTPUT_SCHEMA
    assert task.task_output_schema_checksum == OUTPUT_CHECKSUM
    assert task.workflow == "review-change-v1"
    assert task.view == "standard"
    assert task.default_view == "standard"
    assert task.compatibility_level == "adapted"
    assert task.version == "1.0.0"
    assert task.published_at == PUBLISHED_AT
    requirements = task.capability_blueprint["resource_requirements"]
    assert len(requirements) == 1
    assert requirements[0]["capabilities"] == ["repository.read/v1"]
    assert requirements[0]["required"] is True
    assert requirements[0]["accepted_providers"] == ["git"]
    assert task.tool_requirements[0].capability == "repository.read/v1"


def test_projects_every_task_in_a_multi_task_manifest() -> None:
    """複数 task を持つ Manifest から task 数だけ descriptor を投影する。"""

    manifest = _manifest(
        capabilities=[
            {"key": "repository.review", "title": "Repository Review"},
            {"key": "repository.summarize", "title": "Repository Summary"},
        ],
        tasks=[
            _task("review-change", "repository.review", "wf-a", "standard"),
            _task("summarize-change", "repository.summarize", "wf-b", "standard"),
        ],
    )

    tasks = _project(manifest)

    assert [task.task_key for task in tasks] == ["review-change", "summarize-change"]
    assert tasks[1].title == "Repository Summary"


def test_projects_generated_schema_and_checksum_without_business_reference() -> None:
    """Generated task は inline Schema と checksum を TaskCatalog へそのまま投影する。"""

    manifest = _manifest()

    projected = _project(manifest)[0]

    assert projected.input_schema == INPUT_SCHEMA
    outcome = compile_outcome_schema(OUTPUT_SCHEMA, task_schema_checksum=OUTPUT_CHECKSUM)
    assert projected.output_schema == outcome.schema
    assert projected.input_schema_checksum == INPUT_CHECKSUM
    assert projected.task_output_schema == OUTPUT_SCHEMA

    resolved = _resolve(manifest, "review-change")
    assert resolved is not None
    assert resolved.output_schema == outcome.schema
    assert resolved.output_schema_checksum == outcome.checksum
    assert resolved.task_output_schema_checksum == OUTPUT_CHECKSUM


def test_open_outcome_task_does_not_require_task_specific_output_schema() -> None:
    """開放式 report は task output 契約なしでも通用 OutcomeEnvelope で発見・実行できる。"""

    task = _task("review-change", "repository.review", "review-change-v1", "standard")
    for key in ("output_contract", "output_schema", "output_schema_checksum"):
        task.pop(key)
    manifest = _manifest(tasks=[task])

    projected = _project(manifest)[0]
    expected = compile_outcome_schema(None, task_schema_checksum=None)

    assert projected.output_schema == expected.schema
    assert projected.output_schema_checksum == expected.checksum
    assert projected.task_output_schema is None
    assert projected.task_output_schema_checksum is None
    assert "structured_data" not in projected.output_schema["properties"]


def test_missing_optional_sections_yield_empty_requirements() -> None:
    """tools が無い Manifest でも空 requirement で投影できる。"""

    manifest = _manifest()
    manifest.pop("tools")

    task = _project(manifest)[0]

    assert task.tool_requirements == ()


def test_title_falls_back_to_task_key_without_matching_capability() -> None:
    """Capability title が無い場合は task key を title に使う。"""

    manifest = _manifest(capabilities=[])

    assert _project(manifest)[0].title == "review-change"


def test_malformed_and_empty_tasks_are_skipped() -> None:
    """dict でない task や key/capability を欠く task は投影しない。"""

    assert _project(_manifest(tasks=[])) == []
    assert _project(_manifest(tasks="not-a-list")) == []
    manifest = _manifest(tasks=["not-a-dict", {"key": "no-capability"}, {"capability": "no.key"}])
    assert _project(manifest) == []


def test_legacy_reference_task_is_not_projected_or_resolved() -> None:
    """Generated Schema を持たない Release A task は新規実行 catalog へ公開しない。"""

    manifest = _manifest(
        tasks=[
            {
                "key": "legacy-review",
                "capability": "repository.review",
                "type": "immediate",
                "input_schema": {"$ref": "legacy/input.schema.json"},
                "output_schema": {"$ref": "legacy/output.schema.json"},
                "workflow": "legacy",
                "view": "legacy",
            }
        ]
    )

    assert _project(manifest) == []
    assert _resolve(manifest, "legacy-review") is None


def _resolve(manifest: dict[str, Any], task_key: str) -> Any:
    """固定 identity で単一 task の Run 解決を実行する。"""

    return resolve_task_run_from_manifest(
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="repository-review",
        version="1.0.0",
        manifest_checksum="sha256:" + ("a" * 64),
        manifest=manifest,
        task_key=task_key,
    )


def test_resolve_task_run_binds_exact_version_and_snapshot() -> None:
    """指定 task を精確 version と skill_snapshot 束縛付きで解決する。"""

    resolved = _resolve(_manifest(), "review-change")

    assert resolved is not None
    assert resolved.task_key == "review-change"
    assert resolved.capability == "repository.review"
    assert resolved.input_schema == INPUT_SCHEMA
    assert resolved.input_schema_checksum == INPUT_CHECKSUM
    assert resolved.allowed_capabilities == ("repository.read/v1",)
    assert resolved.skill_snapshot["manifest_checksum"] == "sha256:" + ("a" * 64)
    assert resolved.skill_snapshot["sort_order"] == 0
    assert resolved.skill_snapshot["manifest"]["tasks"][0]["key"] == "review-change"


def test_resolve_task_run_returns_none_for_unknown_task_key() -> None:
    """Manifest に無い task_key は None を返す。"""

    assert _resolve(_manifest(), "does-not-exist") is None


def test_resolve_task_run_unions_tool_and_data_source_capabilities() -> None:
    """allowed_capabilities は tool と data source の capability を安定順で統合する。"""

    manifest = _manifest(
        tools=[
            {"capability": "repository.read/v1", "required": True},
            {"capability": "issue.read/v1", "required": False},
        ],
        data_sources=[
            {
                "key": "issue-source",
                "capability": "issue.read/v1",
                "required": True,
                "accepted_providers": ["csv"],
            }
        ],
    )

    resolved = _resolve(manifest, "review-change")

    assert resolved is not None
    assert resolved.allowed_capabilities == ("issue.read/v1", "repository.read/v1")


def test_descriptor_carries_the_resolved_capability_blueprint() -> None:
    """Task descriptor が就緒度計算に使える蓝图を運ぶ。

    蓝图を持たない旧 Manifest でも互換投影で補い、資源要求の逐項判定を可能にする。
    """

    tasks = _project(_manifest())

    blueprint = tasks[0].capability_blueprint
    assert blueprint["blueprint_version"] == "projectmind.capability-blueprint/v1"
    assert [item["key"] for item in blueprint["resource_requirements"]] == ["repository-source"]
    # 就緒度は Project の資源保有状況に依存するため、投影層では未判定のままにする。
    assert tasks[0].readiness is None
