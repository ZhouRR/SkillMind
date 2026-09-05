"""無 Schema Skill の model 解釈を反復し、脱敏済み安定性 metric を出力する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from jsonschema import ValidationError

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.core.settings import Settings, get_settings
from projectmind.skills import (
    InterpreterFixtureRunner,
    SkillPackageParser,
    SkillStaticAnalyzer,
    UnsafeSkillSourceError,
    build_interpreter_request,
    load_inline_text_files,
)
from projectmind.skills.interpreter_execution import (
    InterpreterExecutionError,
    candidate_repair_feedback,
)
from projectmind.skills.wiring import build_skill_interpreter


@dataclass(frozen=True, slots=True)
class CandidateMetric:
    """一回の interpretation に対する秘密を含まない acceptance metric。"""

    valid: bool
    error_code: str | None
    repair_attempts: int
    semantic_signature: str | None
    contract_signature: str | None
    runtime_binding_signature: str | None
    runtime_binding_dimensions: tuple[str, ...]
    field_paths: tuple[str, ...]
    field_shapes: tuple[str, ...]
    review_item_count: int


@dataclass(frozen=True, slots=True)
class SourceMetric:
    """一つの SkillSource に対する反復結果と集約値。"""

    source: str
    repeats: int
    timeout_seconds: int
    valid_count: int
    structural_valid_rate: float
    semantic_signature_count: int
    semantically_stable: bool
    contract_signature_count: int
    contract_semantically_stable: bool
    runtime_binding_signature_count: int
    runtime_bindings_stable: bool
    candidates: tuple[CandidateMetric, ...]


async def measure_source(
    source: Path,
    *,
    repeats: int,
    timeout_seconds: int,
) -> SourceMetric:
    """一つの directory Skill を同じ frozen request で反復解釈する。"""

    settings = _measurement_settings()
    # Application の Settings と同じ .env を利用する一方、process に注入済みの値は常に優先する。
    # Claude runtime 側の allowlist を経由するため、DB credential 等は model subprocess に渡らない。
    environment_fallback = dotenv_values(".env") if Path(".env").is_file() else {}
    interpreter, catalog, identity, model = build_skill_interpreter(
        settings,
        environment_fallback=environment_fallback,
    )
    if interpreter is None or catalog is None or identity is None or model is None:
        raise RuntimeError("Skill interpreter model configuration is unavailable")
    root = source.resolve(strict=True)
    package = SkillPackageParser().parse_directory(root)
    analysis = SkillStaticAnalyzer().analyze(
        package,
        load_inline_text_files(root, package),
    )
    if analysis.blocked:
        raise UnsafeSkillSourceError("Blocked source cannot enter model stability measurement")
    request = build_interpreter_request(
        package=package,
        analysis=analysis,
        catalog=catalog,
        system_skill=identity,
    )
    runner = InterpreterFixtureRunner(settings.contracts_dir)
    measured: list[CandidateMetric] = []
    for index in range(repeats):
        candidate = await _measure_candidate(
            interpreter=interpreter,
            runner=runner,
            request=request,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        measured.append(candidate)
        # 長い model 呼び出しでも進捗を観測できるよう、本文を含まない metric だけを stderr へ出す。
        print(
            json.dumps(
                {
                    "candidate": index + 1,
                    "error_code": candidate.error_code,
                    "repair_attempts": candidate.repair_attempts,
                    "source": root.name,
                    "valid": candidate.valid,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
    candidates = tuple(measured)
    valid = tuple(candidate for candidate in candidates if candidate.valid)
    signatures = {
        candidate.semantic_signature
        for candidate in valid
        if candidate.semantic_signature is not None
    }
    contract_signatures = {
        candidate.contract_signature
        for candidate in valid
        if candidate.contract_signature is not None
    }
    binding_signatures = {
        candidate.runtime_binding_signature
        for candidate in valid
        if candidate.runtime_binding_signature is not None
    }
    return SourceMetric(
        source=root.name,
        repeats=repeats,
        timeout_seconds=timeout_seconds,
        valid_count=len(valid),
        structural_valid_rate=len(valid) / repeats,
        semantic_signature_count=len(signatures),
        semantically_stable=len(valid) == repeats and len(signatures) == 1,
        contract_signature_count=len(contract_signatures),
        contract_semantically_stable=(
            len(valid) == repeats and len(contract_signatures) == 1
        ),
        runtime_binding_signature_count=len(binding_signatures),
        runtime_bindings_stable=(len(valid) == repeats and len(binding_signatures) == 1),
        candidates=candidates,
    )


def _measurement_settings() -> Settings:
    """Container 用 contract path がない宿主実行では repository asset へ限定して戻す。"""

    settings = get_settings()
    if settings.contracts_dir.is_dir():
        return settings
    repository_contracts = Path(__file__).resolve().parents[1] / "contracts"
    if repository_contracts.is_dir():
        return settings.model_copy(update={"contracts_dir": repository_contracts})
    return settings


async def _measure_candidate(
    *,
    interpreter: Any,
    runner: InterpreterFixtureRunner,
    request: Mapping[str, Any],
    model: str,
    timeout_seconds: int,
) -> CandidateMetric:
    """Production と同じ一回修復を許し、単一候補を deterministic gate へ通す。"""

    validation_feedback: str | None = None
    attempt = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            for attempt in range(2):
                try:
                    candidate = await interpreter.interpret(
                        request,
                        model=model,
                        parameters={},
                        validation_feedback=validation_feedback,
                    )
                    validated = runner.run(request, candidate, bind_identity=True)
                    return _candidate_metric(validated, repair_attempts=attempt)
                except InterpreterExecutionError as error:
                    repair_feedback = candidate_repair_feedback(error.code)
                    if repair_feedback is not None and attempt == 0:
                        validation_feedback = repair_feedback
                        continue
                    return _invalid_candidate(error.code.value, repair_attempts=attempt)
                except (ValidationError, ValueError, KeyError, TypeError) as error:
                    if attempt == 0:
                        # Candidate 値を model や出力へ複製せず、分類だけで完全な再生成を要求する。
                        validation_feedback = f"deterministic_validation:{type(error).__name__}"
                        continue
                    return _invalid_candidate("schema_validation_failed", repair_attempts=attempt)
    except TimeoutError:
        return _invalid_candidate("model_timeout", repair_attempts=attempt)
    return _invalid_candidate("schema_validation_failed", repair_attempts=1)


def _candidate_metric(
    response: Mapping[str, Any], *, repair_attempts: int
) -> CandidateMetric:
    """Validated response から task contract の意味 signature と review 数を抽出する。"""

    manifest = _mapping(response.get("runtime_manifest_draft"))
    report = _mapping(response.get("report"))
    tasks = _object_sequence(manifest.get("tasks"))
    ordered_tasks = sorted(
        tasks,
        key=lambda task: canonical_json(
            {
                "input": _contract_semantics(_mapping(task.get("input_contract"))),
                "output": _contract_semantics(_mapping(task.get("output_contract"))),
                "type": task.get("type"),
            }
        ),
    )
    semantic = {
        "compatibility": _mapping(manifest.get("compatibility")).get("level"),
        # Model が付けた内部識別子や自然言語 title は field semantics ではない。件数と
        # cross-reference 後の構造を比較し、命名揺れを false positive にしない。
        "capability_count": len(_object_sequence(manifest.get("capabilities"))),
        "tasks": [
            {
                "input_contract": _contract_semantics(_mapping(task.get("input_contract"))),
                "output_contract": _contract_semantics(_mapping(task.get("output_contract"))),
                "type": task.get("type"),
            }
            for task in ordered_tasks
        ],
        "data_sources": sorted(
            (
                {
                    "accepted_providers": sorted(_string_sequence(item.get("accepted_providers"))),
                    "capability": item.get("capability"),
                    "required": item.get("required"),
                    "selection_policy": item.get("selection_policy"),
                }
                for item in _object_sequence(manifest.get("data_sources"))
            ),
            key=canonical_json,
        ),
        "tools": sorted(
            (
                {"capability": item.get("capability"), "required": item.get("required")}
                for item in _object_sequence(manifest.get("tools"))
            ),
            key=lambda item: str(item.get("capability")),
        ),
        "workflows": sorted(
            (
                {
                    "steps": [
                        {"kind": step.get("kind")}
                        for step in _object_sequence(workflow.get("steps"))
                    ],
                }
                for workflow in _object_sequence(manifest.get("workflows"))
            ),
            key=canonical_json,
        ),
    }
    contract_semantic = {"tasks": semantic["tasks"]}
    runtime_binding_semantic = {
        key: value for key, value in semantic.items() if key != "tasks"
    }
    field_paths = sorted(
        path
        for index, task in enumerate(ordered_tasks, start=1)
        for contract_name in ("input_contract", "output_contract")
        for path in _contract_field_paths(
            _mapping(task.get(contract_name)),
            prefix=f"task[{index}]/{contract_name}",
        )
    )
    field_shapes = sorted(
        shape
        for index, task in enumerate(ordered_tasks, start=1)
        for contract_name in ("input_contract", "output_contract")
        for shape in _contract_field_shapes(
            _mapping(task.get(contract_name)),
            prefix=f"task[{index}]/{contract_name}",
        )
    )
    review_count = sum(
        len(value) if isinstance(value, list) else 0
        for value in (
            report.get("assumptions"),
            report.get("questions"),
            report.get("diagnostics"),
        )
    )
    return CandidateMetric(
        valid=True,
        error_code=None,
        repair_attempts=repair_attempts,
        semantic_signature=f"sha256:{sha256_hex(canonical_json(semantic))}",
        contract_signature=f"sha256:{sha256_hex(canonical_json(contract_semantic))}",
        runtime_binding_signature=(
            f"sha256:{sha256_hex(canonical_json(runtime_binding_semantic))}"
        ),
        runtime_binding_dimensions=_binding_dimensions(runtime_binding_semantic),
        field_paths=tuple(field_paths),
        field_shapes=tuple(field_shapes),
        review_item_count=review_count,
    )


def _invalid_candidate(error_code: str, *, repair_attempts: int) -> CandidateMetric:
    """失敗候補を source/model output を含まない metric へ畳み込む。"""

    return CandidateMetric(
        valid=False,
        error_code=error_code,
        repair_attempts=repair_attempts,
        semantic_signature=None,
        contract_signature=None,
        runtime_binding_signature=None,
        runtime_binding_dimensions=(),
        field_paths=(),
        field_shapes=(),
        review_item_count=0,
    )


def _contract_field_paths(contract: Mapping[str, Any], *, prefix: str) -> tuple[str, ...]:
    """Nested TaskContractDraft の field path を安定順で列挙する。"""

    paths: list[str] = []
    for field in _object_sequence(contract.get("fields")):
        key = field.get("key")
        if not isinstance(key, str):
            continue
        path = f"{prefix}/{key}"
        paths.append(path)
        paths.extend(_contract_field_paths(field, prefix=path))
        items = _mapping(field.get("items"))
        paths.extend(_contract_field_paths(items, prefix=f"{path}[]"))
    return tuple(paths)


def _contract_semantics(contract: Mapping[str, Any]) -> dict[str, Any]:
    """自然言語 description を除き、検証動作を決める Contract 構造だけを正規化する。"""

    fields = sorted(
        (_field_semantics(field) for field in _object_sequence(contract.get("fields"))),
        key=lambda field: str(field.get("key")),
    )
    return {"fields": fields}


def _field_semantics(field: Mapping[str, Any]) -> dict[str, Any]:
    """Field の key/type/required/enum/nested shape を比較可能な値へ畳み込む。"""

    result: dict[str, Any] = {
        "key": field.get("key"),
        "required": field.get("required"),
        "type": field.get("type"),
    }
    enum = field.get("enum")
    if isinstance(enum, list):
        result["enum"] = sorted(enum, key=canonical_json)
    nested = _contract_semantics(field)
    if nested["fields"]:
        result["fields"] = nested["fields"]
    items = _mapping(field.get("items"))
    if items:
        result["items"] = _field_semantics(items)
    return result


def _contract_field_shapes(contract: Mapping[str, Any], *, prefix: str) -> tuple[str, ...]:
    """本文を出さず、field path・type・required の人手確認用 shape を列挙する。"""

    shapes: list[str] = []
    for field in _object_sequence(contract.get("fields")):
        key = field.get("key")
        if not isinstance(key, str):
            continue
        path = f"{prefix}/{key}"
        required = "required" if field.get("required") is True else "optional"
        constraints = {
            key: field[key]
            for key in (
                "enum",
                "max_length",
                "maximum",
                "min_length",
                "minimum",
                "pattern",
            )
            if key in field
        }
        suffix = (
            f":constraints_sha256={sha256_hex(canonical_json(constraints))[:12]}"
            if constraints
            else ""
        )
        shapes.append(f"{path}:{field.get('type', 'unknown')}:{required}{suffix}")
        shapes.extend(_contract_field_shapes(field, prefix=path))
        items = _mapping(field.get("items"))
        shapes.extend(_contract_field_shapes(items, prefix=f"{path}[]"))
    return tuple(shapes)


def _binding_dimensions(binding: Mapping[str, Any]) -> tuple[str, ...]:
    """Runtime binding の差分箇所を本文なしで確認できる安定 dimension へ変換する。"""

    dimensions = [
        f"compatibility:{binding.get('compatibility')}",
        f"capability_count:{binding.get('capability_count')}",
    ]
    for source in _object_sequence(binding.get("data_sources")):
        providers = ",".join(_string_sequence(source.get("accepted_providers")))
        dimensions.append(
            "data_source:"
            f"{source.get('capability')}:{source.get('required')}:{providers}:"
            f"{source.get('selection_policy')}"
        )
    for tool in _object_sequence(binding.get("tools")):
        dimensions.append(f"tool:{tool.get('capability')}:{tool.get('required')}")
    for workflow in _object_sequence(binding.get("workflows")):
        kinds = ">".join(
            str(step.get("kind")) for step in _object_sequence(workflow.get("steps"))
        )
        dimensions.append(f"workflow:{kinds}")
    return tuple(sorted(dimensions))


def _mapping(value: Any) -> Mapping[str, Any]:
    """Mapping 以外を安全な空 object として扱う。"""

    return value if isinstance(value, Mapping) else {}


def _object_sequence(value: Any) -> list[Mapping[str, Any]]:
    """List 内の object だけを順序を保って返す。"""

    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_sequence(value: Any) -> list[str]:
    """Sequence 内の文字列だけを返し、文字列自身は sequence として扱わない。"""

    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, str)]


async def _run(
    sources: Sequence[Path],
    *,
    repeats: int,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    """複数 source を順に測定し、JSON 化可能な結果を返す。"""

    return [
        asdict(
            await measure_source(
                source,
                repeats=repeats,
                timeout_seconds=timeout_seconds,
            )
        )
        for source in sources
    ]


def main() -> None:
    """CLI 引数を検証し、脱敏済み measurement JSON を標準出力へ書く。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    arguments = parser.parse_args()
    if arguments.repeats < 3:
        parser.error("--repeats must be at least 3")
    if not 30 <= arguments.timeout_seconds <= 900:
        parser.error("--timeout-seconds must be between 30 and 900")
    result = asyncio.run(
        _run(
            arguments.sources,
            repeats=arguments.repeats,
            timeout_seconds=arguments.timeout_seconds,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
