"""CapabilityBlueprint を決定的に正規化・検証し、canonical checksum を固定する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.skills.document_prerequisites import document_prerequisites
from skillmind.skills.task_contract import (
    TaskContractCompilationError,
    compile_task_contract,
)

CAPABILITY_BLUEPRINT_VERSION = "skillmind.capability-blueprint/v1"

_GUIDANCE_SECTIONS = (
    "prohibited_actions",
    "quality_criteria",
    "recommended_steps",
    "required_rules",
)
_OPTIONAL_COLLECTIONS = (
    "assumptions",
    "effect_intents",
    "interaction_points",
    "questions",
    "resource_requirements",
)


class CapabilityBlueprintError(ValueError):
    """安全に公開できる code と path を持つ blueprint 検証失敗。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        """機密値を含まない安定した失敗情報を保持する。"""

        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class CompiledCapabilityBlueprint:
    """正規化済み blueprint と canonical checksum の不変な組。"""

    blueprint: dict[str, Any]
    checksum: str


def resolve_capability_blueprint(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Manifest が宣言した蓝图を返し、無ければ None を返す。

    蓝图は Interpreter の産物である (docs/11 §4)。導入期の決定的 draft は解釈前なので蓝图を
    持たず、そこから機械的に組み立てても Skill が申告していない目標と規則を捏造するだけになる。
    そのため補完はせず「まだ解釈していない」を None として表現する。発行済み manifest に蓝图が
    必須であることは publish gate が保証する。
    """

    declared = manifest.get("capability_blueprint")
    if isinstance(declared, dict):
        return deepcopy(cast(dict[str, Any], declared))
    return None


def bind_blueprint_identity(manifest: dict[str, Any]) -> None:
    """宣言済み蓝图の identity と compatibility を manifest の権威値へ揃える。

    蓝图と RuntimeManifest は同じ解釈の二つの面であり、別々の source_hash、interpretation_id、
    互換 level を持ってはならない。identity は model 解釈時と DRAFT 凍結時の二段で確定するため、
    確定のたびにここを通し、片方だけが更新されて監査値が食い違う状態を作らない。
    """

    blueprint = manifest.get("capability_blueprint")
    if not isinstance(blueprint, dict):
        return
    identity = _mapping(manifest, "identity")
    blueprint["identity"] = {
        key: identity[key]
        for key in ("skill_key", "source_hash", "interpretation_id", "interpreter_version")
        if key in identity
    }
    compatibility = _mapping(manifest, "compatibility")
    projected: dict[str, Any] = {}
    if "level" in compatibility:
        projected["level"] = compatibility["level"]
    confidence = compatibility.get("confidence")
    if isinstance(confidence, int | float) and not isinstance(confidence, bool):
        projected["confidence"] = confidence
    blueprint["compatibility"] = projected


def normalize_capability_blueprint(blueprint: Mapping[str, Any]) -> dict[str, Any]:
    """省略された集合を空値で補い、trace 順を安定化した canonical document を返す。

    Skill と model は任意項目を省略できる。既定値をここへ一箇所に集めることで、同じ
    解釈から常に同じ checksum が得られる。型が壊れた値は矯正せず後段の Schema 検証へ
    渡し、正規化が不正な blueprint を有効に見せてしまう事態を避ける。
    """

    normalized = deepcopy(dict(blueprint))
    for key in _OPTIONAL_COLLECTIONS:
        normalized.setdefault(key, [])
    guidance = normalized.get("guidance")
    if guidance is None or isinstance(guidance, dict):
        section = cast(dict[str, Any], guidance) if isinstance(guidance, dict) else {}
        for name in _GUIDANCE_SECTIONS:
            section.setdefault(name, [])
        normalized["guidance"] = section
    preferences = normalized.get("execution_preferences")
    if preferences is None or isinstance(preferences, dict):
        merged = cast(dict[str, Any], preferences) if isinstance(preferences, dict) else {}
        for name in ("session_split_hints", "stop_conditions"):
            merged.setdefault(name, [])
        # recommended_profile は Skill 側の推奨であり platform 既定ではない。宣言が無いまま
        # SUPERVISED を書き込むと「Skill が推奨した」という誤った事実を凍結してしまうため、
        # ここでは補完せず ExecutionProfilePolicy の既定判断へ委ねる。
        normalized["execution_preferences"] = merged
    for intent in _object_list(normalized.get("effect_intents")):
        # 省略を「承認不要」と解釈させないため、apply の既定承認 ask を検証前に確定させる。
        if intent.get("mode") == "apply":
            intent.setdefault("approval_mode", "ask")
    traces = normalized.get("source_traces")
    if isinstance(traces, list) and all(isinstance(item, dict) for item in traces):
        # trace は他要素から参照されないため、並べ替えても pointer target を壊さない。
        # 出力順の揺れを checksum へ持ち込まないようここで安定化する。
        normalized["source_traces"] = sorted(traces, key=_trace_sort_key)
    return normalized


class CapabilityBlueprintValidator:
    """Blueprint の形状、参照整合性、source trace と effect 境界を決定的に検証する。

    領域 capability が Tool catalog に存在しないことや、業務 Schema を持たないことは
    失敗にしない。docs/05 §8.2 のとおり、それらは就緒度を下げる情報であって発行可否の
    hard gate ではない。
    """

    def __init__(self, contracts_dir: Path) -> None:
        """凍結済み CapabilityBlueprint contract を読み込み Schema 自体も検査する。"""

        root = contracts_dir.resolve(strict=True)
        self._schema = _load_json(root / "capability-blueprint/v1.schema.json")
        Draft202012Validator.check_schema(self._schema)

    def validate(self, blueprint: Mapping[str, Any]) -> CompiledCapabilityBlueprint:
        """正規化 → Schema → 決定的規則の順に検査し、checksum 付き blueprint を返す。"""

        normalized = normalize_capability_blueprint(blueprint)
        self._validate_schema(normalized)
        self._validate_references(normalized)
        self._validate_effect_intents(normalized)
        self._validate_required_rule_traces(normalized)
        self._validate_document_prerequisites(normalized)
        return CompiledCapabilityBlueprint(
            blueprint=normalized,
            checksum=f"sha256:{sha256_hex(canonical_json(normalized))}",
        )

    def _validate_document_prerequisites(self, blueprint: Mapping[str, Any]) -> None:
        """前置条件は既存 apply intent と原文 trace に対応する場合だけ受理する。"""
        targets = {item.get("target") for item in _object_list(blueprint.get("source_traces"))}
        for index, task in enumerate(_object_list(blueprint.get("tasks"))):
            if "document_prerequisites" not in task:
                continue
            path = f"/tasks/{index}/document_prerequisites"
            try:
                document_prerequisites(
                    {"capability_blueprint": blueprint}, str(task.get("key", "")),
                    check_runtime_capability=False,
                )
                if path not in targets:
                    raise ValueError("Document prerequisites require source evidence")
            except ValueError as error:
                raise CapabilityBlueprintError(
                    "document_prerequisites_invalid", path, str(error)
                ) from error

    def _validate_schema(self, blueprint: Mapping[str, Any]) -> None:
        """通用 contract に対する形状違反を path 順の先頭で報告する。"""

        validator = Draft202012Validator(self._schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(blueprint), key=lambda item: list(item.path))
        if errors:
            raise CapabilityBlueprintError(
                "blueprint_schema_invalid",
                _json_path(errors[0].path),
                errors[0].message,
            )

    def _validate_references(self, blueprint: Mapping[str, Any]) -> None:
        """Key 重複と task から capability / resource への参照整合性を検証する。"""

        capabilities = _unique_keys(
            blueprint.get("capabilities"),
            path="/capabilities",
            code="capability_key_duplicate",
        )
        _unique_keys(blueprint.get("tasks"), path="/tasks", code="task_key_duplicate")
        resources = _unique_keys(
            blueprint.get("resource_requirements"),
            path="/resource_requirements",
            code="resource_key_duplicate",
        )
        _unique_keys(
            blueprint.get("interaction_points"),
            path="/interaction_points",
            code="interaction_key_duplicate",
        )
        _unique_keys(
            blueprint.get("effect_intents"),
            path="/effect_intents",
            code="effect_key_duplicate",
        )
        for index, task in enumerate(_object_list(blueprint.get("tasks"))):
            path = f"/tasks/{index}"
            if task.get("capability") not in capabilities:
                raise CapabilityBlueprintError(
                    "task_capability_unknown",
                    f"{path}/capability",
                    "Task capability is not declared in capabilities",
                )
            raw_keys = task.get("resource_keys")
            keys = raw_keys if isinstance(raw_keys, list) else []
            for position, resource_key in enumerate(keys):
                if resource_key not in resources:
                    raise CapabilityBlueprintError(
                        "task_resource_unknown",
                        f"{path}/resource_keys/{position}",
                        "Task resource key is not declared in resource_requirements",
                    )
            _validate_optional_contract(
                task.get("parameter_contract"),
                path=f"{path}/parameter_contract",
                code="parameter_contract_invalid",
            )
            _validate_optional_contract(
                task.get("result_contract"),
                path=f"{path}/result_contract",
                code="result_contract_invalid",
            )

    def _validate_effect_intents(self, blueprint: Mapping[str, Any]) -> None:
        """apply 意図が具体資源と ask 承認に固定されていることを検証する。

        effect intent は要求であって権限ではない。Skill が資源を特定しないまま apply を
        宣言できると Project 側で scope を固定できず、宣言そのものが承認と読める形で凍結
        されてしまう。ここで resource 参照と ask 承認を必須にして境界を保つ。
        """

        resources = _key_index(blueprint.get("resource_requirements"))
        for index, intent in enumerate(_object_list(blueprint.get("effect_intents"))):
            path = f"/effect_intents/{index}"
            mode = intent.get("mode")
            approval = intent.get("approval_mode")
            resource_key = intent.get("resource_key")
            if mode != "apply":
                if approval is not None:
                    raise CapabilityBlueprintError(
                        "effect_approval_mode_inapplicable",
                        f"{path}/approval_mode",
                        f"approval_mode is not valid for effect mode {mode}",
                    )
                if resource_key is not None and resource_key not in resources:
                    raise CapabilityBlueprintError(
                        "effect_resource_unknown",
                        f"{path}/resource_key",
                        "Effect resource key is not declared in resource_requirements",
                    )
                continue
            if not isinstance(resource_key, str):
                raise CapabilityBlueprintError(
                    "effect_resource_missing",
                    f"{path}/resource_key",
                    "An apply effect intent must reference a resource requirement",
                )
            resource = resources.get(resource_key)
            if resource is None:
                raise CapabilityBlueprintError(
                    "effect_resource_unknown",
                    f"{path}/resource_key",
                    "Effect resource key is not declared in resource_requirements",
                )
            if resource.get("access") != "write":
                raise CapabilityBlueprintError(
                    "effect_resource_not_writable",
                    f"{path}/resource_key",
                    "An apply effect intent must reference a write resource requirement",
                )
            if approval != "ask":
                raise CapabilityBlueprintError(
                    "effect_approval_mode_invalid",
                    f"{path}/approval_mode",
                    "An apply effect intent must keep the ask approval mode",
                )

    def _validate_required_rule_traces(self, blueprint: Mapping[str, Any]) -> None:
        """required rule が source trace を持つことを検証する。

        required rule は Agent が飛ばせない業務強制である。根拠のない追加を許すと
        Interpreter の推測がそのまま規則へ昇格するため、trace の存在を必須にする。
        """

        targets = {
            trace.get("target") for trace in _object_list(blueprint.get("source_traces"))
        }
        guidance = blueprint.get("guidance")
        rules = (
            _object_list(guidance.get("required_rules"))
            if isinstance(guidance, Mapping)
            else []
        )
        for index in range(len(rules)):
            target = f"/guidance/required_rules/{index}"
            if target not in targets:
                raise CapabilityBlueprintError(
                    "required_rule_source_trace_missing",
                    target,
                    "A required rule must cite a source trace",
                )


def _validate_optional_contract(
    contract: Any, *, path: str, code: str
) -> None:
    """宣言された任意 contract draft が compile 可能かだけを確認する。

    Blueprint は draft を凍結し、実行可能 Schema への compile は publish 投影が行う。
    ここで compile 可能性を確かめないと、壊れた draft が発行後に初めて露見する。
    """

    if contract is None:
        return
    if not isinstance(contract, Mapping):
        raise CapabilityBlueprintError(code, path, "Task contract draft must be an object")
    try:
        compile_task_contract(contract)
    except TaskContractCompilationError as error:
        raise CapabilityBlueprintError(code, f"{path}{error.path}", error.message) from error


def _unique_keys(value: Any, *, path: str, code: str) -> dict[str, dict[str, Any]]:
    """Array 内の key 重複を拒否し、key → item の索引を返す。"""

    index: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(_object_list(value)):
        key = item.get("key")
        if not isinstance(key, str):
            continue
        if key in index:
            raise CapabilityBlueprintError(
                code, f"{path}/{position}/key", f"Duplicate key: {key}"
            )
        index[key] = item
    return index


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Manifest の object field を空 object fallback 付きで返す。"""

    item = value.get(key)
    return cast(dict[str, Any], item) if isinstance(item, dict) else {}


def _key_index(value: Any) -> dict[str, dict[str, Any]]:
    """重複検査を伴わない key → item の索引を返す。"""

    return {
        key: item
        for item in _object_list(value)
        if isinstance((key := item.get("key")), str)
    }


def _object_list(value: Any) -> list[dict[str, Any]]:
    """Blueprint array から object item だけを返す。"""

    return (
        [cast(dict[str, Any], item) for item in value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value)
        else []
    )


def _trace_sort_key(item: Mapping[str, Any]) -> tuple[str, str, int, str]:
    """Source trace を target、path、line、reason で安定順化する。"""

    line = item.get("line")
    return (
        str(item.get("target", "")),
        str(item.get("path", "")),
        line if isinstance(line, int) and not isinstance(line, bool) else 0,
        str(item.get("reason", "")),
    )


def _json_path(path: Any) -> str:
    """jsonschema error path を JSON Pointer 風の診断 path へ変換する。"""

    return "/" + "/".join(str(item) for item in path)


def _load_json(path: Path) -> dict[str, Any]:
    """Contract path から JSON object だけを読み込む。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Contract must be a JSON object: {path}")
    return cast(dict[str, Any], value)
