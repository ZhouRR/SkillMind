"""Project に依存しない原設計門禁を、全 Task と保存 source の両面から検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.skills.design_validation import (
    SkillDesignInvalidError,
    SkillDesignSource,
    validate_skill_design,
)
from skillmind.skills.task_contract import compile_task_contract
from skillmind.skills.task_flow_preview import (
    TaskFlowPreviewInvalidError,
    project_task_flow_preview,
)
from tests.skills.task_flow_fixtures import make_task_flow_source

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _source() -> SkillDesignSource:
    """共有合成原値から Project/版の展示 identity を除き、組織設計だけを作る。"""

    original = make_task_flow_source()
    return SkillDesignSource(
        skill_key=original.skill_key,
        manifest_checksum=original.manifest_checksum,
        manifest=original.manifest,
        source_hash=original.source_hash,
        source_file_index=original.source_file_index,
        source_snapshot=original.source_snapshot,
        interpretation_id=original.interpretation_id,
        interpreter_version=original.interpreter_version,
    )


def _rehashed(source: SkillDesignSource) -> SkillDesignSource:
    """一箇所の不整合を独立検証するため、合成 Manifest の hash だけを更新する。"""

    return replace(
        source, manifest_checksum="sha256:" + sha256_hex(canonical_json(source.manifest))
    )


def _with_binary_source(source: SkillDesignSource) -> SkillDesignSource:
    """Blob を読まず検証できる index-only file を合成し、原 index hash 形式で束縛する。"""

    source.source_file_index.insert(
        0,
        {
            "path": "attachment.bin",
            "mime": "application/octet-stream",
            "size": 7,
            "sha256": "sha256:" + "b" * 64,
            "binary": True,
        },
    )
    source_hash = "sha256:" + sha256_hex(
        json.dumps(source.source_file_index, sort_keys=True, separators=(",", ":"))
    )
    source.manifest["identity"]["source_hash"] = source_hash
    source.manifest["capability_blueprint"]["identity"]["source_hash"] = source_hash
    return _rehashed(replace(source, source_hash=source_hash))


def test_design_requires_no_project_or_version_identity_and_preserves_original_values() -> None:
    """新 DRAFT に架空 Project/SkillVersion を要求せず、原 JSON と旧 source hash を保つ。"""

    source = _source()
    before = canonical_json(source.manifest)
    result = validate_skill_design(source=source, contracts_dir=CONTRACTS)
    assert {item.name for item in fields(source)} == {
        "skill_key",
        "manifest_checksum",
        "manifest",
        "source_hash",
        "source_file_index",
        "source_snapshot",
        "interpretation_id",
        "interpreter_version",
    }
    assert canonical_json(result.manifest) == before
    assert result.blueprint == source.manifest["capability_blueprint"]
    assert all(trace["verification"] == "TEXT_SNAPSHOT" for trace in result.source_traces)
    assert "Explain input" not in repr(result)
    assert "規則を読む" not in repr(source)
    result.manifest["tasks"].clear()
    assert canonical_json(source.manifest) == before
    source.source_snapshot[0]["content"] = "changed original"
    assert result.source_traces[0]["line"] == 1


@pytest.mark.parametrize("declared", [False, True])
def test_blueprint_is_required_unless_readonly_legacy_preview_explicitly_allows_absence(
    declared: bool,
) -> None:
    """発行の既定経路は未宣言を拒否し、旧 preview の許可だけが source 未検証を保持する。"""

    source = _source()
    if declared:
        source.manifest["capability_blueprint"] = None
    else:
        del source.manifest["capability_blueprint"]
    source = _rehashed(replace(source, source_file_index=None, source_snapshot="unverified"))
    with pytest.raises(SkillDesignInvalidError) as captured:
        validate_skill_design(source=source, contracts_dir=CONTRACTS)
    assert captured.value.code == "capability_blueprint_missing"
    result = validate_skill_design(
        source=source, contracts_dir=CONTRACTS, allow_missing_blueprint=True
    )
    assert result.blueprint is None and result.source_traces == ()


@pytest.mark.parametrize(
    "field", ["skill_key", "source_hash", "interpretation_id", "interpreter_version"]
)
def test_blueprint_original_identity_is_not_rebound_to_hide_mismatch(field: str) -> None:
    """読み取り門禁は platform の新規 binding と区別し、不一致を上書き修正しない。"""

    source = _source()
    source.manifest["capability_blueprint"]["identity"][field] = "different-original"
    source = _rehashed(source)
    before = canonical_json(source.manifest)
    with pytest.raises(SkillDesignInvalidError) as captured:
        validate_skill_design(source=source, contracts_dir=CONTRACTS)
    assert captured.value.code == "skill_design_invalid"
    assert str(captured.value) == "The saved skill design is invalid."
    assert captured.value.__cause__ is None
    assert canonical_json(source.manifest) == before


@pytest.mark.parametrize("damage", ["missing", "extra", "different_key", "different_capability"])
def test_all_task_correspondence_includes_tasks_not_selected_for_preview(damage: str) -> None:
    """選択 Task が正常でも、他 Task の集合/能力の不一致を共通入口で拒否する。"""

    source = _source()
    blueprint = source.manifest["capability_blueprint"]
    if damage == "missing":
        source.manifest["tasks"].pop(0)
    elif damage == "extra":
        task = deepcopy(source.manifest["tasks"][0])
        task["key"] = "extra"
        source.manifest["tasks"].append(task)
    elif damage == "different_key":
        blueprint["tasks"][0]["key"] = "different"
    else:
        source.manifest["tasks"][0]["capability"] = "other.capability"
    source = _rehashed(source)
    with pytest.raises(SkillDesignInvalidError):
        validate_skill_design(source=source, contracts_dir=CONTRACTS)
    original = make_task_flow_source()
    preview_source = replace(
        original, **{field.name: getattr(source, field.name) for field in fields(source)}
    )
    with pytest.raises(TaskFlowPreviewInvalidError):
        project_task_flow_preview(
            source=preview_source, task_key="explain", contracts_dir=CONTRACTS
        )


def test_existing_generated_capability_alias_is_preserved_for_every_task() -> None:
    """Manifest の既存 skill.task alias を原 Blueprint capability と同一文字列へ書き換えない。"""

    source = _source()
    for task in source.manifest["tasks"]:
        task["capability"] = f"{source.skill_key}.{task['key']}"
    source = _rehashed(source)
    result = validate_skill_design(source=source, contracts_dir=CONTRACTS)
    assert result.manifest["tasks"][0]["capability"] == "reading-guide.other"
    assert result.blueprint is not None
    assert result.blueprint["tasks"][0]["capability"] == "reading.explain"


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "missing.md"),
        ("path", "../secret.md"),
        ("target", "/missing"),
        ("line", 100000),
        ("line", True),
    ],
)
def test_blueprint_traces_reference_real_safe_source_and_existing_original_targets(
    field: str,
    value: object,
) -> None:
    """Schema だけでは分からない元 file/JSON target/行を、保存本文に対して照合する。"""

    source = _source()
    source.manifest["capability_blueprint"]["source_traces"][0][field] = value
    with pytest.raises(SkillDesignInvalidError):
        validate_skill_design(source=_rehashed(source), contracts_dir=CONTRACTS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_path", "missing.md"),
        ("source_path", "../outside"),
        ("source_path", "https://example.invalid/source"),
        ("line", 10000),
        ("line", True),
        ("contract", "output"),
    ],
)
def test_every_manifest_contract_trace_requires_a_real_declared_contract_and_source(
    field: str,
    value: object,
) -> None:
    """公開されない別 Task の契約 trace でも、不存在 output/file/行を見逃さない。"""

    source = _source()
    source.manifest["tasks"][0]["contract_source_trace"][0][field] = value
    with pytest.raises(SkillDesignInvalidError):
        validate_skill_design(source=_rehashed(source), contracts_dir=CONTRACTS)


@pytest.mark.parametrize(
    "field_path", ["/", "/target_path", "/rows/0/value", "original description"]
)
def test_contract_field_path_keeps_existing_unresolved_vocabulary(field_path: str) -> None:
    """Schema の原文語彙だけを保ち、未定義の業務 field/index 意味を新しい拒否条件にしない。"""

    source = _source()
    trace = source.manifest["tasks"][1]["contract_source_trace"][0]
    trace["field_path"] = field_path
    trace["source_section"] = "Original section description"
    trace.pop("line")
    result = validate_skill_design(source=_rehashed(source), contracts_dir=CONTRACTS)
    assert result.manifest["tasks"][1]["contract_source_trace"][0] == trace
    assert "verification" not in result.manifest["tasks"][1]["contract_source_trace"][0]


def test_declared_output_contract_trace_is_verified_without_mutating_its_optional_fields() -> None:
    """本当に宣言された output だけを認め、source_section/null と原 root sentinel を保持する。"""

    source = _source()
    task = source.manifest["tasks"][0]
    draft: dict[str, Any] = {
        "contract_version": "skillmind.task-contract-draft/v1",
        "type": "string",
    }
    compiled = compile_task_contract(draft)
    task.update(
        output_contract=draft,
        output_schema=compiled.schema,
        output_schema_checksum=compiled.checksum,
    )
    trace = {
        "contract": "output",
        "field_path": "/",
        "source_path": "説明/SKILL.md",
        "line": None,
        "source_section": None,
    }
    task["contract_source_trace"].append(trace)
    result = validate_skill_design(source=_rehashed(source), contracts_dir=CONTRACTS)
    assert result.manifest["tasks"][0]["contract_source_trace"][-1] == trace


@pytest.mark.parametrize("line", [None, 1])
def test_binary_contract_trace_is_index_only_and_cannot_claim_a_text_line(line: int | None) -> None:
    """Binary file の index 存在は確認するが、blob も文字行も読んだことにしない。"""

    source = _with_binary_source(_source())
    source.manifest["tasks"][0]["contract_source_trace"][0].update(
        source_path="attachment.bin", line=line
    )
    source = _rehashed(source)
    if line is not None:
        with pytest.raises(SkillDesignInvalidError):
            validate_skill_design(source=source, contracts_dir=CONTRACTS)
    else:
        validate_skill_design(source=source, contracts_dir=CONTRACTS)


@pytest.mark.parametrize(
    "damage", ["snapshot", "index_hash", "nil_interpretation", "manifest_hash"]
)
def test_saved_source_and_identity_damage_fails_without_repair(damage: str) -> None:
    """整った source 字面だけではなく、元 size/hash/非 nil identity の一致を必須とする。"""

    source = _source()
    if damage == "snapshot":
        source.source_snapshot[0]["content"] += "changed bytes"
    elif damage == "index_hash":
        source.source_file_index[0]["mime"] = "text/plain"
    elif damage == "nil_interpretation":
        source = replace(source, interpretation_id=UUID(int=0))
    else:
        source = replace(source, manifest_checksum="sha256:" + "0" * 64)
    with pytest.raises(SkillDesignInvalidError):
        validate_skill_design(source=source, contracts_dir=CONTRACTS)
