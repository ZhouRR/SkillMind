"""二つの無 Schema Skill を公開 API だけで完全な lifecycle へ通す。"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from jsonschema import Draft202012Validator

from projectmind.ops.smoke import (
    SmokeApiClient,
    SmokeTask,
    _read_smoke_email,
    _read_smoke_password,
    _run_happy_path,
    resolve_smoke_project_id,
)

_TERMINAL_INTERPRET_EVENTS = frozenset({"interpret.completed", "interpret.failed"})
_ADJUSTMENT_INSTRUCTION = (
    "Preserve every source-derived task, contract field, required flag, tool, data source, "
    "workflow, and permission boundary unchanged. Re-evaluate the interpretation without "
    "adding inferred fields or permissions."
)
_FIXTURE_PROVIDERS = {
    "issue.read/v1": "csv",
    "repository.read/v1": "git",
}


@dataclass(frozen=True)
class AcceptanceScenario:
    """一つの Skill source と、生成 Schema へ投入する意味的 fixture 値を保持する。"""

    name: str
    directory: str
    input_hints: Mapping[str, Any]


_SCENARIOS = (
    AcceptanceScenario(
        name="jaf-ticket-quality",
        directory="jaf-ticket-quality",
        input_hints={
            "ticket_id": "fixture-001",
            "report_language": "English",
        },
    ),
    AcceptanceScenario(
        name="repository-review",
        directory="repository-review",
        input_hints={
            "revision": "1111111111111111111111111111111111111111",
            "path": "src/example.py",
            "review_focus": "correctness",
        },
    ),
)


def _load_source_files(root: Path) -> list[dict[str, str]]:
    """Acceptance source の UTF-8 text file を相対 path 順に読み込む。"""

    if not root.is_dir():
        raise RuntimeError(f"Acceptance Skill directory is missing: {root.name}")
    files: list[dict[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(
                f"Acceptance Skill contains a non-text file: {root.name}/{relative}"
            ) from error
        files.append({"path": relative, "content": content})
    if not files:
        raise RuntimeError(f"Acceptance Skill directory is empty: {root.name}")
    return files


def _required_string(value: Mapping[str, Any], key: str, *, label: str) -> str:
    """公開 API response の必須文字列を秘密を含めず検証する。"""

    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise RuntimeError(f"{label} did not contain {key}")
    return result


def _wait_for_interpretation(
    client: SmokeApiClient,
    launch: Mapping[str, Any],
    *,
    deadline: float,
) -> dict[str, Any]:
    """Stored/queued の双方を正本 execution detail へ収束させる。"""

    execution = launch.get("execution")
    if launch.get("status") == "stored" and isinstance(execution, dict):
        interpretation_id = _required_string(
            execution, "interpretation_id", label="Stored interpretation"
        )
    elif launch.get("status") == "queued":
        execution_key = _required_string(launch, "execution_key", label="Interpret launch")
        terminal = client.wait_for_sse_event(
            f"/api/v1/skill-interpretations/stream/{execution_key}",
            event_field="event",
            event_names=_TERMINAL_INTERPRET_EVENTS,
            deadline=deadline,
        )
        data = terminal.get("data")
        if not isinstance(data, dict) or terminal.get("event") != "interpret.completed":
            error_code = data.get("error_code") if isinstance(data, dict) else None
            raise RuntimeError(f"Interpretation did not complete: {error_code or 'unknown'}")
        interpretation_id = _required_string(
            data, "interpretation_id", label="Interpret terminal event"
        )
    else:
        raise RuntimeError("Interpret launch did not match stored/queued contract")
    detail = client.request_json(
        f"/api/v1/skill-interpretations/{interpretation_id}/execution"
    )
    if detail.get("status") != "PREVIEW_READY":
        raise RuntimeError(f"Interpretation is not preview-ready: {detail.get('status')}")
    return detail


def _schema_sample(schema: Mapping[str, Any], hints: Mapping[str, Any], *, path: str) -> Any:
    """生成 Schema の必要 field だけを、source 別の意味値と安全な既定値から構築する。"""

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        hint = hints.get(path.rsplit("/", 1)[-1])
        return hint if hint in enum else enum[0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    field_name = path.rsplit("/", 1)[-1]
    hint = hints.get(field_name)
    if hint is not None:
        return hint
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise RuntimeError(f"Generated object Schema is incomplete at {path}")
        result: dict[str, Any] = {}
        for key, child in properties.items():
            if not isinstance(key, str) or not isinstance(child, dict):
                raise RuntimeError(f"Generated property Schema is invalid at {path}")
            if key in required or key in hints:
                result[key] = _schema_sample(child, hints, path=f"{path}/{key}")
        return result
    if kind == "array":
        minimum = schema.get("minItems", 0)
        if not isinstance(minimum, int) or minimum <= 0:
            return []
        items = schema.get("items")
        if not isinstance(items, dict):
            raise RuntimeError(f"Generated array Schema is incomplete at {path}")
        return [_schema_sample(items, hints, path=f"{path}/0") for _ in range(minimum)]
    if kind == "string":
        return "acceptance-value"
    if kind == "integer":
        minimum = schema.get("minimum", 0)
        return int(minimum) if isinstance(minimum, (int, float)) else 0
    if kind == "number":
        minimum = schema.get("minimum", 0)
        return float(minimum) if isinstance(minimum, (int, float)) else 0.0
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    raise RuntimeError(f"Generated Schema has unsupported type at {path}: {kind}")


def _task_from_catalog(
    catalog: Mapping[str, Any],
    *,
    skill_version_id: str,
    scenario: AcceptanceScenario,
) -> SmokeTask:
    """公開済み精確 version の単一 task を探し、有効な Run fixture を生成する。"""

    raw_tasks = catalog.get("tasks")
    if not isinstance(raw_tasks, list):
        raise RuntimeError("Task catalog did not contain a tasks array")
    matches = [
        item
        for item in raw_tasks
        if isinstance(item, dict) and item.get("skill_version_id") == skill_version_id
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Acceptance Skill must publish exactly one task: {scenario.name} ({len(matches)})"
        )
    task = cast(dict[str, Any], matches[0])
    input_schema = task.get("input_schema")
    if not isinstance(input_schema, dict):
        raise RuntimeError("Published task did not contain an inline input Schema")
    input_json = _schema_sample(input_schema, scenario.input_hints, path="$")
    if not isinstance(input_json, dict):
        raise RuntimeError("Acceptance task input Schema root must be an object")
    errors = list(Draft202012Validator(input_schema).iter_errors(input_json))
    if errors:
        raise RuntimeError("Generated acceptance input did not match the published Schema")
    sources: dict[str, str] = {}
    # 資源要求の宣言元は蓝图のみ。readiness は同じ要求を Project 文脈付きで投影したもの。
    readiness = task.get("readiness")
    requirements = readiness.get("requirements") if isinstance(readiness, dict) else None
    if not isinstance(requirements, list):
        raise RuntimeError("Published task did not contain resource requirements")
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise RuntimeError("Published task contained an invalid resource requirement")
        declared = requirement.get("capabilities")
        capabilities = declared if isinstance(declared, list) else []
        provider = next(
            (
                candidate
                for capability in capabilities
                if (candidate := _FIXTURE_PROVIDERS.get(str(capability))) is not None
            ),
            None,
        )
        key = requirement.get("key")
        if provider is not None:
            source_key = _required_string(requirement, "key", label="Resource requirement")
            sources[source_key] = provider
        elif requirement.get("required") is True:
            raise RuntimeError(f"No acceptance fixture provider is available for {key}")
    if not sources:
        raise RuntimeError(f"Acceptance Skill did not bind any evidence source: {scenario.name}")
    return SmokeTask(
        skill_version_id=skill_version_id,
        task_key=_required_string(task, "task_key", label="Published task"),
        input_json=input_json,
        sources=sources,
    )


def _accept_scenario(
    client: SmokeApiClient,
    project_id: UUID,
    skills_root: Path,
    scenario: AcceptanceScenario,
    *,
    deadline: float,
) -> None:
    """Import から Result/Evidence まで一つの source を公開 API で検証する。"""

    imported = client.request_json(
        "/api/v1/skill-imports",
        method="POST",
        body={"files": _load_source_files(skills_root / scenario.directory)},
    )
    source_id = _required_string(imported, "skill_source_id", label="Skill import")
    launch = client.request_json(
        f"/api/v1/skill-sources/{source_id}/interpret"
        "?force_regenerate=true",
        method="POST",
    )
    interpreted = _wait_for_interpretation(client, launch, deadline=deadline)
    parent_id = _required_string(interpreted, "interpretation_id", label="Interpretation")
    adjusted_launch = client.request_json(
        f"/api/v1/skill-interpretations/{parent_id}/adjust",
        method="POST",
        body={"instruction": _ADJUSTMENT_INSTRUCTION},
    )
    adjusted = _wait_for_interpretation(client, adjusted_launch, deadline=deadline)
    if adjusted.get("parent_interpretation_id") != parent_id:
        raise RuntimeError("Adjusted interpretation did not preserve append-only lineage")
    adjusted_id = _required_string(adjusted, "interpretation_id", label="Adjustment")
    draft = client.request_json(
        f"/api/v1/skill-interpretations/{adjusted_id}/draft",
        method="POST",
    )
    findings = draft.get("gate_findings")
    if draft.get("status") != "DRAFT" or draft.get("gate_passed") is not True:
        raise RuntimeError("Adjusted interpretation did not create a publishable DRAFT")
    if not isinstance(findings, list):
        raise RuntimeError("DRAFT did not contain gate findings")
    warnings = [
        item.get("code")
        for item in findings
        if isinstance(item, dict) and item.get("severity") == "warning"
    ]
    if warnings:
        raise RuntimeError(f"Acceptance DRAFT requires unexpected warnings: {warnings}")
    skill_version_id = _required_string(draft, "skill_version_id", label="Skill DRAFT")
    published = client.request_json(
        f"/api/v1/skill-versions/{skill_version_id}/publish",
        method="POST",
        body={"accepted_warnings": []},
    )
    if published.get("status") != "PUBLISHED":
        raise RuntimeError("SkillVersion did not reach PUBLISHED")
    # 新規公開は既定でどの Project にも見えない。Acceptance 対象 Project へ精確版を明示的に
    # 有効化してから TaskCatalog/Run の経路を検証する。
    client.request_json(
        f"/api/v1/projects/{project_id}/skill-versions/{skill_version_id}",
        method="PUT",
    )
    catalog = client.request_json(f"/api/v1/projects/{project_id}/tasks")
    task = _task_from_catalog(
        catalog, skill_version_id=skill_version_id, scenario=scenario
    )
    _run_happy_path(client, project_id, task, deadline)
    print(f"dynamic-contract: PASSED ({scenario.name})")


def main() -> None:
    """環境設定を読み、二つの無 Schema source の完全 lifecycle を検証する。"""

    base_url = os.getenv("PROJECTMIND_SMOKE_BASE_URL", "http://127.0.0.1:8000")
    timeout_seconds = int(os.getenv("PROJECTMIND_ACCEPTANCE_TIMEOUT_SECONDS", "3600"))
    project_id = resolve_smoke_project_id()
    skills_root = Path(
        os.getenv("PROJECTMIND_ACCEPTANCE_SKILLS_DIR", "/app/skills/examples")
    ).resolve()
    client = SmokeApiClient(base_url)
    client.login(email=_read_smoke_email(), password=_read_smoke_password())
    deadline = time.monotonic() + timeout_seconds
    for scenario in _SCENARIOS:
        _accept_scenario(client, project_id, skills_root, scenario, deadline=deadline)
    print("ProjectMind dynamic Task Contract acceptance passed")


if __name__ == "__main__":
    main()
