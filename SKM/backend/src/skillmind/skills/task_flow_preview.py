"""凍結 Manifest を変更せず、単一 Task の宣言と Skill 共通事項を読み取り投影する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.domain import derive_task_id
from skillmind.skills.design_validation import (
    SkillDesignInvalidError,
    SkillDesignSource,
    is_valid_skill_design_key,
    validate_skill_design,
)
from skillmind.skills.domain import PublishedTaskNotFoundError

TASK_FLOW_PREVIEW_VERSION = "skillmind.task-flow-preview/v1"
_SHARED_FIELDS = (
    "guidance",
    "interaction_points",
    "effect_intents",
    "execution_preferences",
    "assumptions",
    "questions",
)


class TaskFlowPreviewInvalidError(ValueError):
    """保存内容を安全なプレビューとして確認できないことだけを公開する。"""

    def __init__(self) -> None:
        """元の source、JSON path や検証例外を外部へ持ち出さない。"""

        super().__init__("The saved task flow preview is invalid.")


@dataclass(frozen=True, slots=True)
class TaskFlowPreviewSource:
    """Loader が同一組織・有効化・FK を確認した精確版の原値。

    JSON は coercion 済み DTO でなく保存列を渡す。公開前の defensive copy と内容照合は
    projector が担当し、Loader の認可や関係照合をこの純関数で代替しない。
    """

    project_id: UUID
    skill_id: UUID
    skill_version_id: UUID
    skill_key: str
    version: str
    manifest_checksum: str
    manifest: dict[str, Any] = field(repr=False)
    skill_source_id: UUID
    source_hash: str
    source_file_index: Any = field(repr=False)
    source_snapshot: Any = field(repr=False)
    interpretation_id: UUID
    interpreter_version: str


@dataclass(frozen=True, slots=True)
class TaskFlowPreview:
    """Readiness や実行事実を含めない、版付きの読み取り専用 response。"""

    identity: dict[str, Any]
    status: Literal["AVAILABLE", "NOT_DECLARED"]
    blueprint_checksum: str | None
    preview_checksum: str
    plan: dict[str, Any] | None = field(repr=False)
    source_traces: tuple[dict[str, Any], ...] = field(repr=False)
    preview_version: str = TASK_FLOW_PREVIEW_VERSION

    def to_json(self) -> dict[str, Any]:
        """公開 field を明示し、consumer の変更を元 DTO へ戻さない。"""

        return deepcopy(
            {
                "preview_version": self.preview_version,
                "identity": self.identity,
                "status": self.status,
                "blueprint_checksum": self.blueprint_checksum,
                "preview_checksum": self.preview_checksum,
                "plan": self.plan,
                "source_traces": list(self.source_traces),
            }
        )


def project_task_flow_preview(
    *, source: TaskFlowPreviewSource, task_key: str, contracts_dir: Path
) -> TaskFlowPreview:
    """原 JSON の identity/hash/ref を照合し、実行計画や権限を新設せず投影する。"""

    try:
        return _project(source, task_key=task_key, contracts_dir=contracts_dir)
    except (
        SkillDesignInvalidError,
        TypeError,
        ValueError,
        KeyError,
        RecursionError,
        OverflowError,
    ):
        # Validator の診断は本文や保存 path を含み得るため、公開例外に連結しない。
        raise TaskFlowPreviewInvalidError() from None


def _project(
    source: TaskFlowPreviewSource, *, task_key: str, contracts_dir: Path
) -> TaskFlowPreview:
    """認可後に渡された値だけを扱い、source storage や最新 version を再解決しない。"""

    for value in (
        source.project_id,
        source.skill_id,
        source.skill_version_id,
        source.skill_source_id,
        source.interpretation_id,
    ):
        _require(isinstance(value, UUID) and value.int != 0)
    _require(isinstance(source.version, str) and 0 < len(source.version) <= 32)
    _require(is_valid_skill_design_key(task_key))
    design = validate_skill_design(
        source=SkillDesignSource(
            skill_key=source.skill_key,
            manifest_checksum=source.manifest_checksum,
            manifest=source.manifest,
            source_hash=source.source_hash,
            source_file_index=source.source_file_index,
            source_snapshot=source.source_snapshot,
            interpretation_id=source.interpretation_id,
            interpreter_version=source.interpreter_version,
        ),
        contracts_dir=contracts_dir,
        allow_missing_blueprint=True,
    )
    manifest_tasks = {task["key"]: task for task in design.manifest["tasks"]}
    if task_key not in manifest_tasks:
        raise PublishedTaskNotFoundError("Published task not found")
    identity = {
        "project_id": str(source.project_id),
        "skill_id": str(source.skill_id),
        "skill_version_id": str(source.skill_version_id),
        "task_id": str(derive_task_id(skill_version_id=source.skill_version_id, task_key=task_key)),
        "task_key": task_key,
        "skill_key": source.skill_key,
        "version": source.version,
        "manifest_checksum": source.manifest_checksum,
    }
    blueprint = design.blueprint
    if blueprint is None:
        return _result(identity, blueprint=None, plan=None, traces=())
    tasks = {task["key"]: (index, task) for index, task in enumerate(blueprint["tasks"])}
    task_index, task = tasks[task_key]
    traces = design.source_traces
    resources = {
        resource["key"]: (index, resource)
        for index, resource in enumerate(blueprint.get("resource_requirements", []))
    }
    selected_keys = task.get("resource_keys", [])
    selected = [
        _reference("TASK", f"/resource_requirements/{resources[key][0]}", resources[key][1])
        for key in selected_keys
    ]
    shared = {
        "scope": "SKILL",
        "resource_requirements": [
            _reference("SKILL", f"/resource_requirements/{index}", value)
            for key, (index, value) in resources.items()
            if key not in selected_keys
        ],
        **{name: deepcopy(blueprint.get(name)) for name in _SHARED_FIELDS},
    }
    plan = {
        "task": _reference("TASK", f"/tasks/{task_index}", task),
        "task_resources": selected,
        "shared": shared,
    }
    return _result(identity, blueprint=blueprint, plan=plan, traces=traces)


def _result(
    identity: dict[str, Any],
    *,
    blueprint: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    traces: tuple[dict[str, Any], ...],
) -> TaskFlowPreview:
    """原語義と検査範囲だけを hash し、変動する readiness は含めない。"""

    status: Literal["AVAILABLE", "NOT_DECLARED"] = (
        "AVAILABLE" if blueprint is not None else "NOT_DECLARED"
    )
    blueprint_checksum = _checksum(blueprint) if blueprint is not None else None
    body = {
        "preview_version": TASK_FLOW_PREVIEW_VERSION,
        "identity": identity,
        "status": status,
        "blueprint_checksum": blueprint_checksum,
        "plan": plan,
        "source_traces": list(traces),
    }
    return TaskFlowPreview(
        identity=identity,
        status=status,
        blueprint_checksum=blueprint_checksum,
        preview_checksum=_checksum(body),
        plan=plan,
        source_traces=traces,
    )


def _reference(scope: str, pointer: str, value: dict[str, Any]) -> dict[str, Any]:
    """Source pointer は参照であり、未来の plan node ID として発号しない。"""

    return {"scope": scope, "blueprint_ref": pointer, "value": deepcopy(value)}


def _checksum(value: Any) -> str:
    """新しい読み取り投影だけを共通 canonical JSON で hash する。"""

    return f"sha256:{sha256_hex(canonical_json(value))}"


def _require(condition: bool) -> None:
    """不整合な値を coercion や部分成功へ変換しない。"""

    if not condition:
        raise TaskFlowPreviewInvalidError()
