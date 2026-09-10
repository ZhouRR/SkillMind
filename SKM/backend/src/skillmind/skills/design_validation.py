"""原 Skill 宣言と保存 source を、Project や公開表示に依存せず検証する。"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.evaluations.domain import InvalidEvaluationRevisionError, resolve_json_pointer
from skillmind.skills.capability_blueprint import (
    CapabilityBlueprintError,
    CapabilityBlueprintValidator,
)
from skillmind.skills.importer import SkillImportLimits

_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_GUIDANCE_FIELDS = ("required_rules", "recommended_steps", "quality_criteria", "prohibited_actions")


class SkillDesignInvalidError(ValueError):
    """元の本文や validator 例外を公開せず、静的な拒否理由だけを保持する。"""

    def __init__(
        self,
        code: Literal[
            "skill_design_invalid", "capability_blueprint_missing"
        ] = "skill_design_invalid",
    ) -> None:
        """既存の未宣言 finding と損傷を区別し、source の値を message に含めない。"""

        super().__init__("The saved skill design is invalid.")
        self.code = code


@dataclass(frozen=True, slots=True)
class SkillDesignSource:
    """認可済み loader が渡す原値。Organization/Project/状態の認可は代替しない。"""

    skill_key: str
    manifest_checksum: str
    manifest: dict[str, Any] = field(repr=False)
    source_hash: str
    source_file_index: Any = field(repr=False)
    source_snapshot: Any = field(repr=False)
    interpretation_id: UUID
    interpreter_version: str


@dataclass(frozen=True, slots=True)
class ValidatedSkillDesign:
    """原宣言の defensive copy と確認できた source trace のみを返す。"""

    manifest: dict[str, Any] = field(repr=False)
    blueprint: dict[str, Any] | None = field(repr=False)
    source_traces: tuple[dict[str, Any], ...] = field(repr=False)


def validate_skill_design(
    *,
    source: SkillDesignSource,
    contracts_dir: Path,
    allow_missing_blueprint: bool = False,
) -> ValidatedSkillDesign:
    """保存 hash・元 identity・全 Task と source の一致を、補完や書込なしで検査する。"""

    try:
        return _validate(
            source, contracts_dir=contracts_dir, allow_missing_blueprint=allow_missing_blueprint
        )
    except SkillDesignInvalidError:
        raise
    except (
        CapabilityBlueprintError,
        InvalidEvaluationRevisionError,
        TypeError,
        ValueError,
        KeyError,
        RecursionError,
        OverflowError,
    ):
        raise SkillDesignInvalidError() from None


def _validate(
    source: SkillDesignSource, *, contracts_dir: Path, allow_missing_blueprint: bool
) -> ValidatedSkillDesign:
    """Draft と既発行版の共通原値を扱い、Project や新しい解釈へ fallback しない。"""

    _require(isinstance(source.interpretation_id, UUID) and source.interpretation_id.int != 0)
    _require(is_valid_skill_design_key(source.skill_key))
    _require(isinstance(source.interpreter_version, str) and bool(source.interpreter_version))
    _require(_valid_hash(source.manifest_checksum) and _valid_hash(source.source_hash))
    manifest = deepcopy(source.manifest)
    _require(isinstance(manifest, dict))
    _require(_checksum(manifest) == source.manifest_checksum)
    _require(manifest.get("manifest_version") == "skillmind/v1alpha1")
    expected_identity = {
        "skill_key": source.skill_key,
        "source_hash": source.source_hash,
        "interpretation_id": str(source.interpretation_id),
        "interpreter_version": source.interpreter_version,
    }
    _require(manifest.get("identity") == expected_identity)
    manifest_tasks = _keyed(manifest.get("tasks"), maximum=50)
    blueprint = manifest.get("capability_blueprint")
    if blueprint is None:
        if not allow_missing_blueprint:
            raise SkillDesignInvalidError("capability_blueprint_missing")
        # 旧プレビューは未宣言とだけ表示する。壊れた source を検証済みへ格上げしない。
        return ValidatedSkillDesign(manifest=manifest, blueprint=None, source_traces=())
    _require(isinstance(blueprint, dict))
    _validate_raw_schema(manifest, contracts_dir / "runtime-manifest/v1alpha1.schema.json")
    _validate_raw_schema(blueprint, contracts_dir / "capability-blueprint/v1.schema.json")
    _require(blueprint.get("identity") == expected_identity)
    compatibility = manifest.get("compatibility")
    blueprint_compatibility = blueprint.get("compatibility")
    if not isinstance(compatibility, dict) or not isinstance(blueprint_compatibility, dict):
        raise SkillDesignInvalidError()
    _require(blueprint_compatibility.get("level") == compatibility.get("level"))
    # 検査用 normalizer の既定値を、原 Manifest/Blueprint へ書き戻さない。
    _check_declared_types(blueprint)
    CapabilityBlueprintValidator(contracts_dir).validate(blueprint)
    _check_all_keys(blueprint)
    tasks = _keyed(blueprint["tasks"], maximum=50)
    _require(set(tasks) == set(manifest_tasks))
    for task_key, (_, task) in tasks.items():
        _, manifest_task = manifest_tasks[task_key]
        _require(
            manifest_task.get("capability")
            in {task["capability"], f"{source.skill_key}.{task_key}"}
        )
    files = _source_files(source)
    traces = tuple(_trace(blueprint, trace, files) for trace in blueprint["source_traces"])
    _check_contract_traces(manifest, files)
    return ValidatedSkillDesign(manifest=manifest, blueprint=blueprint, source_traces=traces)


def _check_contract_traces(
    manifest: dict[str, Any], files: dict[str, tuple[dict[str, Any], str | None]]
) -> None:
    """生成契約の出典も同じ保存 file/行門禁へ通し、schema 文言を source と誤認しない。"""

    for task in manifest["tasks"]:
        for trace in task["contract_source_trace"]:
            _require(f"{trace['contract']}_contract" in task)
            # field_path は既存 producer の '/' を含む原文として保持する。業務 field の
            # 配列位置や source_section の意味は未定義なので、解決済みとは主張しない。
            _source_location(trace["source_path"], trace.get("line"), files)


def _source_location(
    path: Any,
    line: Any,
    files: dict[str, tuple[dict[str, Any], str | None]],
) -> Literal["TEXT_SNAPSHOT", "SOURCE_INDEX"]:
    """Blueprint と契約 trace の双方で、安全な原 file/行の存在だけを確認する。"""

    normalized = _source_path(path)
    _require(normalized in files)
    _, content = files[normalized]
    _require(line is None or (type(line) is int and line > 0))
    if content is None:
        _require(line is None)
        return "SOURCE_INDEX"
    if line is not None:
        _require(line <= max(1, len(content.splitlines())))
    return "TEXT_SNAPSHOT"


def _check_declared_types(blueprint: dict[str, Any]) -> None:
    """正規化が明示 null を空集合に変えて損傷を隠すことを防ぐ。"""

    for name in ("guidance", "execution_preferences"):
        if name in blueprint:
            _require(isinstance(blueprint[name], dict))
    for name in (
        "resource_requirements",
        "interaction_points",
        "effect_intents",
        "assumptions",
        "questions",
    ):
        if name in blueprint:
            _require(isinstance(blueprint[name], list))


def _check_all_keys(blueprint: dict[str, Any]) -> None:
    """個別 collection の key 重複を拒否し、異種 scope の同名までは禁じない。"""

    for name in (
        "capabilities",
        "tasks",
        "resource_requirements",
        "interaction_points",
        "effect_intents",
        "questions",
    ):
        _keyed(blueprint.get(name, []), maximum=50)
    _keyed(blueprint.get("assumptions", []), maximum=100)
    for name in _GUIDANCE_FIELDS:
        _keyed(blueprint.get("guidance", {}).get(name, []), maximum=100)
    for name in ("session_split_hints", "stop_conditions"):
        _keyed(blueprint.get("execution_preferences", {}).get(name, []), maximum=100)
    for task in blueprint["tasks"]:
        for name in ("success_criteria", "deliverables"):
            _keyed(task.get(name, []), maximum=50)


def _source_files(source: SkillDesignSource) -> dict[str, tuple[dict[str, Any], str | None]]:
    """保存 index の旧 hash と任意 text snapshot を照合する。Blob の存在は調べない。"""

    limits = SkillImportLimits()
    index = deepcopy(source.source_file_index)
    _require(isinstance(index, list) and 0 < len(index) <= limits.max_files)
    files: dict[str, tuple[dict[str, Any], str | None]] = {}
    total = 0
    for item in index:
        _require(
            isinstance(item, dict) and set(item) == {"path", "mime", "size", "sha256", "binary"}
        )
        path = _source_path(item["path"])
        _require(path not in files)
        _require(type(item["size"]) is int and 0 <= item["size"] <= limits.max_file_bytes)
        _require(type(item["binary"]) is bool and _valid_hash(item["sha256"]))
        _require(isinstance(item["mime"], str) and 0 < len(item["mime"]) <= 255)
        total += item["size"]
        _require(total <= limits.max_total_bytes)
        files[path] = item, None
    _require(list(files) == sorted(files))
    # importer.py の永続化済み形式は ensure_ascii=True。共通 canonical_json に置換すると
    # 日本語/中国語 path の過去 hash が変わるため、この歴史形式だけを明示して保持する。
    index_json = json.dumps(index, sort_keys=True, separators=(",", ":"), allow_nan=False)
    _require(f"sha256:{sha256_hex(index_json)}" == source.source_hash)
    snapshot = deepcopy(source.source_snapshot)
    _require(isinstance(snapshot, list) and len(snapshot) <= limits.max_files)
    seen: set[str] = set()
    for item in snapshot:
        _require(isinstance(item, dict) and set(item) == {"path", "content"})
        path = _source_path(item["path"])
        _require(path in files and path not in seen and isinstance(item["content"], str))
        seen.add(path)
        record, _ = files[path]
        _require(record["binary"] is False)
        content = item["content"]
        _require(len(content) <= limits.max_file_bytes)
        raw = content.encode("utf-8")
        _require(len(raw) == record["size"] and f"sha256:{sha256_hex(raw)}" == record["sha256"])
        files[path] = record, content
    _require(all(record["binary"] or content is not None for record, content in files.values()))
    return files


def _trace(
    blueprint: dict[str, Any],
    trace: dict[str, Any],
    files: dict[str, tuple[dict[str, Any], str | None]],
) -> dict[str, Any]:
    """JSON Pointer、保存 file 名、確認できる原文行だけを検証する。意味の正しさは証明しない。"""

    resolve_json_pointer(blueprint, trace["target"])
    verification = _source_location(trace["path"], trace["line"], files)
    return {**deepcopy(trace), "verification": verification}


def _source_path(value: Any) -> str:
    """保存 path を修正せず検査し、URI・制御文字・曖昧な相対 path を拒否する。"""

    _require(isinstance(value, str) and 0 < len(value) <= 1024)
    _require(not any(character in value for character in ("\\", ":")))
    _require(all(character.isprintable() for character in value))
    _require(all(part not in {"", ".", ".."} for part in value.split("/")))
    value.encode("utf-8")
    return str(value)


def _keyed(value: Any, *, maximum: int) -> dict[str, tuple[int, dict[str, Any]]]:
    """厳密な配列 shape と key 一意性を確認し、原位置を pointer 用にだけ保存する。"""

    _require(isinstance(value, list) and len(value) <= maximum)
    result: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, item in enumerate(value):
        _require(isinstance(item, dict) and is_valid_skill_design_key(item.get("key")))
        key = item["key"]
        _require(key not in result)
        result[key] = index, item
    return result


def _validate_raw_schema(value: dict[str, Any], path: Path) -> None:
    """原保存値を schema へ直接通し、必須 field の欠落を補完で隠さない。"""

    schema = json.loads(path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    _require(next(validator.iter_errors(value), None) is None)


def is_valid_skill_design_key(value: Any) -> bool:
    """原宣言と読取対象が共有する key alphabet を検査し、値を補正しない。"""

    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def _valid_hash(value: Any) -> bool:
    """保存 hash の表記まで固定する。"""

    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _checksum(value: Any) -> str:
    """新しい読み取り投影だけを共通 canonical JSON で hash する。"""

    return f"sha256:{sha256_hex(canonical_json(value))}"


def _require(condition: bool) -> None:
    """不整合な値を coercion や部分成功へ変換しない。"""

    if not condition:
        raise SkillDesignInvalidError()
