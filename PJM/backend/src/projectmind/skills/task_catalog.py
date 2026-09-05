"""PUBLISHED SkillVersion の Manifest から通用 task descriptor を投影する。

TaskCatalog は JAF 固有の定数を認識しない。Manifest の tasks/capabilities/
blueprint 資源要求/tools/ui を汎用に投影し、精確な version 束縛だけを保持する。実際の
権限付与や「最新版」解決は行わず、発見のための read model を返すことに徹する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from projectmind.agent.outcome import compile_outcome_schema
from projectmind.runs.domain import derive_task_id
from projectmind.skills.capability_blueprint import resolve_capability_blueprint
from projectmind.skills.resource_binding import TaskReadiness, is_write_capability


@dataclass(frozen=True, slots=True)
class TaskToolRequirement:
    """Task が要求する Tool capability と必須性。"""

    capability: str
    required: bool


@dataclass(frozen=True, slots=True)
class ResolvedTaskRun:
    """PUBLISHED task を Run 作成用に精確 version へ束縛した frozen 解決結果。"""

    skill_id: UUID
    skill_version_id: UUID
    skill_key: str
    version: str
    task_key: str
    capability: str
    task_type: str
    manifest_checksum: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_schema_checksum: str
    output_schema_checksum: str
    task_output_schema: dict[str, Any] | None
    task_output_schema_checksum: str | None
    allowed_capabilities: tuple[str, ...]
    skill_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PublishedTaskDescriptor:
    """PUBLISHED SkillVersion の一つの実行可能 task を精確 version へ束縛した read model。"""

    skill_id: UUID
    skill_version_id: UUID
    skill_key: str
    skill_name: str
    version: str
    task_key: str
    # Run 側と同じ決定的 ID。画面が task と Run 履歴を突き合わせる join key になる。
    task_id: UUID
    capability: str
    title: str
    task_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    input_schema_checksum: str
    output_schema_checksum: str
    task_output_schema: dict[str, Any] | None
    task_output_schema_checksum: str | None
    workflow: str
    view: str
    default_view: str
    compatibility_level: str
    tool_requirements: tuple[TaskToolRequirement, ...]
    published_at: datetime | None
    capability_blueprint: dict[str, Any]
    # 就緒度は Project の資源保有状況に依存するため、DB を持たない投影層では決められない。
    # Catalog を配線した service が evaluate_blueprint_readiness で埋める。
    readiness: TaskReadiness | None = None


def project_published_tasks(
    *,
    skill_id: UUID,
    skill_version_id: UUID,
    skill_key: str,
    skill_name: str,
    version: str,
    published_at: datetime | None,
    manifest: Mapping[str, Any],
) -> list[PublishedTaskDescriptor]:
    """Manifest の tasks を汎用 descriptor へ投影する。JAF 定数には依存しない。

    Manifest は publish gate を通過済みで契約に整合するが、破損行でも例外を投げず
    投影可能な範囲だけを返すよう防御的に読み取る。
    """

    titles = _capability_titles(manifest)
    compatibility_level = _string_at(manifest, ("compatibility", "level"))
    default_view = _string_at(manifest, ("ui", "default_view"))
    tools = _tool_requirements(manifest)
    blueprint = resolve_capability_blueprint(manifest)
    if blueprint is None:
        # publish gate が蓝图を必須にしているため、ここへ来る manifest は必ず持っている。
        # 万一欠けた行を discovery へ出すと、目標も規則も無い task が実行できてしまう。
        return []
    descriptors: list[PublishedTaskDescriptor] = []
    for task in _sequence(manifest.get("tasks")):
        if not isinstance(task, Mapping):
            continue
        task_key = task.get("key")
        capability = task.get("capability")
        if not isinstance(task_key, str) or not isinstance(capability, str):
            continue
        contracts = _generated_contracts(task)
        if contracts is None:
            # Release C 以降は generated contract のない旧 Version を discovery へ出さない。
            continue
        (
            input_schema,
            input_checksum,
            task_output_schema,
            task_output_checksum,
        ) = contracts
        outcome = compile_outcome_schema(
            task_output_schema,
            task_schema_checksum=task_output_checksum,
        )
        descriptors.append(
            PublishedTaskDescriptor(
                skill_id=skill_id,
                skill_version_id=skill_version_id,
                skill_key=skill_key,
                skill_name=skill_name,
                version=version,
                task_key=task_key,
                task_id=derive_task_id(
                    skill_version_id=skill_version_id, task_key=task_key
                ),
                capability=capability,
                title=titles.get(capability) or task_key,
                task_type=_string(task.get("type")) or "immediate",
                input_schema=input_schema,
                output_schema=outcome.schema,
                input_schema_checksum=input_checksum,
                output_schema_checksum=outcome.checksum,
                task_output_schema=task_output_schema,
                task_output_schema_checksum=task_output_checksum,
                workflow=_string(task.get("workflow")),
                view=_string(task.get("view")),
                default_view=default_view,
                compatibility_level=compatibility_level,
                tool_requirements=tools,
                published_at=published_at,
                capability_blueprint=deepcopy(blueprint),
            )
        )
    return descriptors


def resolve_task_run_from_manifest(
    *,
    skill_id: UUID,
    skill_version_id: UUID,
    skill_key: str,
    version: str,
    manifest_checksum: str,
    manifest: Mapping[str, Any],
    task_key: str,
) -> ResolvedTaskRun | None:
    """PUBLISHED Manifest から単一 task を Run 作成用の解決結果へ投影する。

    task_key が Manifest に存在しなければ None を返す。RunSkillSnapshot 束縛用の
    skill_snapshot をそのまま同梱し、精確 version と manifest checksum を固定する。
    """

    task = _find_task(manifest, task_key)
    if task is None:
        return None
    capability = task.get("capability")
    if not isinstance(capability, str):
        return None
    contracts = _generated_contracts(task)
    if contracts is None:
        return None
    input_schema, input_checksum, task_output_schema, task_output_checksum = contracts
    outcome = compile_outcome_schema(
        task_output_schema,
        task_schema_checksum=task_output_checksum,
    )
    return ResolvedTaskRun(
        skill_id=skill_id,
        skill_version_id=skill_version_id,
        skill_key=skill_key,
        version=version,
        task_key=task_key,
        capability=capability,
        task_type=_string(task.get("type")) or "immediate",
        manifest_checksum=manifest_checksum,
        input_schema=input_schema,
        output_schema=outcome.schema,
        input_schema_checksum=input_checksum,
        output_schema_checksum=outcome.checksum,
        task_output_schema=task_output_schema,
        task_output_schema_checksum=task_output_checksum,
        allowed_capabilities=_allowed_capabilities(manifest),
        skill_snapshot={
            "skill_version_id": str(skill_version_id),
            "version": version,
            "manifest_checksum": manifest_checksum,
            "manifest": dict(manifest),
            "sort_order": 0,
            "config_snapshot": {},
        },
    )


def _find_task(manifest: Mapping[str, Any], task_key: str) -> Mapping[str, Any] | None:
    """Manifest.tasks から指定 key の task を取り出す。"""

    for task in _sequence(manifest.get("tasks")):
        if isinstance(task, Mapping) and task.get("key") == task_key:
            return task
    return None


def _allowed_capabilities(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Tool と資源要求の capability を安定順の Agent 許可集合へ畳み込む。

    この集合は Agent の Tool 闸门 (`agent/tool_policy.py`) だけが参照する。資源要求の宣言を
    blueprint へ一本化した結果、write 資源の要求は apply capability も併記するため、ここで
    write を除外する。除外しないと Agent が承認経路を通さず apply Tool を直接呼べてしまう
    (apply は承認済み EffectExecution Worker だけが Provider を起動する)。
    """

    capabilities: set[str] = set()
    for item in _sequence(manifest.get("tools")):
        if isinstance(item, Mapping):
            capability = item.get("capability")
            if isinstance(capability, str):
                capabilities.add(capability)
    blueprint = manifest.get("capability_blueprint")
    requirements = (
        _sequence(blueprint.get("resource_requirements"))
        if isinstance(blueprint, Mapping)
        else []
    )
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        for capability in _sequence(requirement.get("capabilities")):
            if isinstance(capability, str) and not is_write_capability(capability):
                capabilities.add(capability)
    return tuple(sorted(capabilities))


def _capability_titles(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Capability key から表示 title への写像を作る。"""

    titles: dict[str, str] = {}
    for capability in _sequence(manifest.get("capabilities")):
        if not isinstance(capability, Mapping):
            continue
        key = capability.get("key")
        title = capability.get("title")
        if isinstance(key, str) and isinstance(title, str):
            titles[key] = title
    return titles


def _tool_requirements(manifest: Mapping[str, Any]) -> tuple[TaskToolRequirement, ...]:
    """Manifest レベルの Tool requirement を型付きへ投影する。"""

    result: list[TaskToolRequirement] = []
    for item in _sequence(manifest.get("tools")):
        if not isinstance(item, Mapping):
            continue
        capability = item.get("capability")
        if not isinstance(capability, str):
            continue
        result.append(
            TaskToolRequirement(capability=capability, required=bool(item.get("required", False)))
        )
    return tuple(result)


def _generated_contracts(
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any] | None, str | None] | None:
    """必須 input と任意 output の生成契約を防御的に投影する。"""

    input_schema = task.get("input_schema")
    output_schema = task.get("output_schema")
    input_checksum = task.get("input_schema_checksum")
    output_checksum = task.get("output_schema_checksum")
    if (
        not isinstance(input_schema, Mapping)
        or "$ref" in input_schema
        or not isinstance(input_checksum, str)
    ):
        return None
    if output_schema is None and output_checksum is None:
        task_output_schema: dict[str, Any] | None = None
        task_output_checksum: str | None = None
    elif (
        isinstance(output_schema, Mapping)
        and "$ref" not in output_schema
        and isinstance(output_checksum, str)
    ):
        task_output_schema = deepcopy(dict(output_schema))
        task_output_checksum = output_checksum
    else:
        # 部分的な任意契約は publish gate 破損として discovery へ出さない。
        return None
    return (
        deepcopy(dict(input_schema)),
        input_checksum,
        task_output_schema,
        task_output_checksum,
    )


def _string(value: Any) -> str:
    """String 以外を空文字へ畳み込む。"""

    return value if isinstance(value, str) else ""


def _string_at(manifest: Mapping[str, Any], path: tuple[str, ...]) -> str:
    """Nested object から string field を安全にたどる。"""

    current: Any = manifest
    for key in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _sequence(value: Any) -> Sequence[Any]:
    """List 状の値だけを列挙対象として返し、str/bytes を除外する。"""

    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()
