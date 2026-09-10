"""原 source を伴う単一 publish gate が損傷や暗黙の補修を拒否する。"""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.skills.design_validation import SkillDesignSource
from projectmind.skills.manifest_gate import ManifestValidator
from tests.skills.test_manifest_gate import ROOT, _generated_manifest, _source_context


def _rehash(source: SkillDesignSource) -> SkillDesignSource:
    """原内容の意味検査を hash 不一致だけで隠さないよう、候補 hash を作る。"""

    return replace(
        source, manifest_checksum="sha256:" + sha256_hex(canonical_json(source.manifest))
    )


def _assert_invalid(source: SkillDesignSource, code: str = "skill_design_invalid") -> None:
    """拒否が静的であり、元データも保存 hash も変更しないことを確認する。"""

    original = deepcopy(source)
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(source)

    assert passed is False
    assert [(item.code, item.severity, item.message, item.path) for item in findings] == [
        (code, "error", "The saved skill design is invalid.", None)
    ]
    assert source == original


def test_only_complete_source_context_can_request_a_publish_decision() -> None:
    """古い構文専用呼出や source 省略を発行可能な別入口として残さない。"""

    signature = inspect.signature(ManifestValidator.evaluate)

    assert tuple(signature.parameters) == ("self", "source")
    assert signature.parameters["source"].default is inspect.Parameter.empty


@pytest.mark.parametrize("location", ["manifest", "blueprint"])
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("skill_key", "other-skill"),
        ("source_hash", "sha256:" + "c" * 64),
        ("interpretation_id", "00000000-0000-4000-8000-000000000999"),
        ("interpreter_version", "different-interpreter/1.0.0"),
    ],
)
def test_identity_drift_is_rejected_even_with_recomputed_manifest_hash(
    location: str, field: str, replacement: str
) -> None:
    """Blueprint と Manifest のどちらも原 source/解釈 identity に固定する。"""

    source = _source_context(_generated_manifest())
    target = source.manifest if location == "manifest" else source.manifest["capability_blueprint"]
    target["identity"][field] = replacement

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("manifest_checksum", "sha256:" + "0" * 64),
        ("source_hash", "sha256:" + "0" * 64),
        ("skill_key", "another-skill"),
        ("interpretation_id", UUID(int=0)),
        ("interpretation_id", uuid4()),
        ("interpreter_version", "other/2.0.0"),
        ("source_file_index", []),
        ("source_snapshot", []),
        ("source_snapshot", None),
    ],
)
def test_original_loader_context_cannot_be_omitted_or_replaced(
    field: str, replacement: Any
) -> None:
    """原 loader の独立値と hash の照合を Manifest 自己申告で代替しない。"""

    source = _source_context(_generated_manifest())

    _assert_invalid(replace(source, **{field: replacement}))


@pytest.mark.parametrize("field", ["guidance", "resource_requirements"])
def test_blueprint_required_fields_are_not_recreated(field: str) -> None:
    """Blueprint normalizer が補える欠損でも、凍結された原宣言を拒否する。"""

    source = _source_context(_generated_manifest())
    del source.manifest["capability_blueprint"][field]

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize("field", ["guidance", "execution_preferences", "effect_intents"])
def test_explicit_null_is_not_normalized_to_empty_declarations(field: str) -> None:
    """明示 null は省略と異なり、空 object/list に変換して通過させない。"""

    source = _source_context(_generated_manifest())
    source.manifest["capability_blueprint"][field] = None

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize("present", [False, True])
def test_missing_or_null_blueprint_retains_existing_missing_finding(present: bool) -> None:
    """parse DRAFT の未宣言を損傷と区別するが、発行判断は必ず拒否する。"""

    source = _source_context(_generated_manifest())
    if present:
        source.manifest["capability_blueprint"] = None
    else:
        del source.manifest["capability_blueprint"]

    _assert_invalid(_rehash(source), "capability_blueprint_missing")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("target", "/absent/900"),
        ("target", "/tasks/900"),
        ("path", "../../outside-synthetic"),
        ("path", "https://invalid.example/source"),
        ("path", "missing.md"),
        ("line", 999999),
        ("line", True),
        ("line", 1.0),
    ],
)
def test_blueprint_trace_must_resolve_original_target_and_file(
    field: str, replacement: Any
) -> None:
    """pointer の形だけでなく原 Blueprint と実 index/text の存在を検査する。"""

    source = _source_context(_generated_manifest())
    source.manifest["capability_blueprint"]["source_traces"][0][field] = replacement

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("source_path", "missing.md"), ("line", 999999), ("line", True)],
)
def test_generated_contract_source_trace_uses_same_real_file_gate(
    field: str, replacement: Any
) -> None:
    """動的 Schema の出典も架空 file や本文外の行を受理しない。"""

    source = _source_context(_generated_manifest())
    source.manifest["tasks"][0]["contract_source_trace"][0][field] = replacement

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize("field", ["size", "sha256", "path", "content"])
def test_source_snapshot_and_original_index_are_not_trusted_without_verification(
    field: str,
) -> None:
    """原本文・byte 数・hash・path の一箇所だけの改変も発行前に止める。"""

    source = _source_context(_generated_manifest())
    if field == "content":
        source.source_snapshot[0][field] += "Changed original text"
    elif field == "size":
        source.source_file_index[0][field] += 1
    elif field == "sha256":
        source.source_file_index[0][field] = "sha256:" + "0" * 64
    else:
        source.source_file_index[0][field] = "other.md"

    _assert_invalid(source)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("external_write_policy", "allow"),
        ("write_capabilities", ["repository.write/v1"]),
        ("network_scope", "unrestricted"),
        ("default_tool_policy", "allow"),
    ],
)
def test_frozen_permission_escalation_is_rejected_not_silently_reset(
    field: str, replacement: Any
) -> None:
    """normalizer に安全値へ書き戻させず、不正な原権限そのものを hard deny する。"""

    source = _source_context(_generated_manifest())
    source.manifest["permissions"][field] = replacement

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize("replacement", [True, 1.0])
def test_generated_schema_comparison_does_not_confuse_boolean_or_float_with_integer(
    replacement: bool | float,
) -> None:
    """Python の true==1==1.0 を Schema の原 JSON 型一致と取り違えない。"""

    source = _source_context(_generated_manifest())
    schema = source.manifest["tasks"][0]["input_schema"]
    schema["properties"]["target_path"]["minLength"] = replacement

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(_rehash(source))

    assert passed is False
    assert "task_contract_schema_mismatch" in {item.code for item in findings}


def test_explicit_empty_catalog_does_not_fall_back_to_default_registered_tools() -> None:
    """明示的に無い capability を platform catalog の既定値で増やさない。"""

    source = _source_context(_generated_manifest())
    source.manifest["tools"] = [{"capability": "workspace.read/v1", "required": True}]
    validator = ManifestValidator(ROOT / "contracts", registered_capabilities=frozenset())

    passed, findings = validator.evaluate(_rehash(source))

    assert validator.registered_capabilities == frozenset()
    assert passed is False
    assert "tool_capability_unregistered" in {item.code for item in findings}


def test_valid_source_does_not_gain_default_fields_or_change_checksum() -> None:
    """省略可能な表示字段は不足を補造せず、原 content/hash をそのまま残す。"""

    source = _source_context(_generated_manifest())
    del source.manifest["tasks"][0]["view"]
    del source.manifest["ui"]
    del source.manifest["tests"]
    source = _rehash(source)
    original = deepcopy(source)

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(source)

    assert passed is True, findings
    assert source == original
    assert "view" not in source.manifest["tasks"][0]


def test_source_optional_asset_is_verified_then_checked_without_becoming_hard_requirement() -> None:
    """原 index/hash と合う任意 fixture は通し、不正 JSON は warning に留める。"""

    source = _source_context(_generated_manifest())
    content = "not a JSON fixture"
    source.source_snapshot.append({"path": "optional.json", "content": content})
    source.source_file_index.append(
        {
            "path": "optional.json",
            "mime": "application/json",
            "size": len(content.encode()),
            "sha256": "sha256:" + sha256_hex(content),
            "binary": False,
        }
    )
    source_hash = "sha256:" + sha256_hex(
        json.dumps(source.source_file_index, sort_keys=True, separators=(",", ":"))
    )
    source.manifest["identity"]["source_hash"] = source_hash
    source.manifest["capability_blueprint"]["identity"]["source_hash"] = source_hash
    source.manifest["tests"] = [{"key": "optional", "type": "schema", "fixture": "optional.json"}]
    source = _rehash(replace(source, source_hash=source_hash))

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(source)

    assert passed is True
    assert [(item.code, item.severity) for item in findings] == [
        ("optional_asset_invalid", "warning")
    ]


@pytest.mark.parametrize("mutation", ["different_task", "extra_task", "duplicate_step"])
def test_all_task_correspondence_and_note_keys_are_checked(mutation: str) -> None:
    """選択 Task 一個だけの成功や同名 note の曖昧さで版全体を発行しない。"""

    source = _source_context(_generated_manifest())
    blueprint = source.manifest["capability_blueprint"]
    if mutation == "different_task":
        blueprint["tasks"][0]["key"] = "other-task"
    elif mutation == "extra_task":
        blueprint["tasks"].append({**blueprint["tasks"][0], "key": "other-task"})
    else:
        blueprint["guidance"]["recommended_steps"] = [
            {"key": "read", "text": "Read the original evidence"},
            {"key": "read", "text": "A conflicting second step"},
        ]

    _assert_invalid(_rehash(source))


@pytest.mark.parametrize("field", ["capabilities", "workflows"])
def test_missing_frozen_bindings_are_not_silently_reconstructed(field: str) -> None:
    """保存版から消えた実行 binding を Task 名から再生成して発行しない。"""

    source = _source_context(_generated_manifest())
    del source.manifest[field]
    source = _rehash(source)
    original = deepcopy(source)

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(source)

    assert passed is False
    expected = "task_capability_missing" if field == "capabilities" else "task_workflow_missing"
    assert expected in {item.code for item in findings}
    assert source == original


def test_no_business_output_contract_still_allows_an_open_outcome() -> None:
    """汎用 Outcome の Task に optional business output Schema を要求しない。"""

    source = _source_context(_generated_manifest())
    task = source.manifest["tasks"][0]
    for name in ("output_contract", "output_schema", "output_schema_checksum"):
        del task[name]
    task["contract_source_trace"] = [
        trace for trace in task["contract_source_trace"] if trace["contract"] == "input"
    ]
    source = _rehash(source)

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(source)

    assert passed is True, findings
    assert "output_contract" not in task
