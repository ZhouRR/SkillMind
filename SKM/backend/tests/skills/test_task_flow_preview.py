"""単一 Task の原宣言、厳密な参照と旧 source hash の読み取り投影を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.domain import derive_task_id
from skillmind.skills.domain import PublishedTaskNotFoundError
from skillmind.skills.task_flow_preview import (
    TaskFlowPreview,
    TaskFlowPreviewInvalidError,
    TaskFlowPreviewSource,
    project_task_flow_preview,
)
from tests.skills.task_flow_fixtures import make_task_flow_source

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _hash(value: Any) -> str:
    """プレビューと原 Manifest の共通 hash を独立に計算する。"""

    return f"sha256:{sha256_hex(canonical_json(value))}"


def _index_hash(value: Any) -> str:
    """Importer の既存 ensure_ascii=True 形式を fixture の正本として保つ。"""

    return "sha256:" + sha256_hex(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _source() -> TaskFlowPreviewSource:
    """公開 API と同じ合成 source を各 case に独立して与える。"""

    return make_task_flow_source()


def _project(source: TaskFlowPreviewSource) -> TaskFlowPreview:
    """外部 service を持たない実 projector を呼び出す。"""

    return project_task_flow_preview(source=source, task_key="explain", contracts_dir=CONTRACTS)


def _rehashed(source: TaskFlowPreviewSource) -> TaskFlowPreviewSource:
    """原 Manifest hash を揃え、内側の独立した不整合だけを検証できるようにする。"""

    return replace(source, manifest_checksum=_hash(source.manifest))


def _rebound_index(source: TaskFlowPreviewSource) -> TaskFlowPreviewSource:
    """型/path 検査を素通ししないよう、壊した index 自体の hash を正確に付け直す。"""

    source_hash = _index_hash(source.source_file_index)
    source.manifest["identity"]["source_hash"] = source_hash
    source.manifest["capability_blueprint"]["identity"]["source_hash"] = source_hash
    return _rehashed(replace(source, source_hash=source_hash))


def test_exact_task_shared_scope_and_original_resource_order_are_preserved() -> None:
    """Task 固有の選択順と共有補集合を分け、他 Task の trace を捨てない。"""

    source = _source()
    original = deepcopy(source)
    result = _project(source)
    body = result.to_json()
    assert body["status"] == "AVAILABLE"
    assert body["identity"]["task_id"] == str(
        derive_task_id(skill_version_id=source.skill_version_id, task_key="explain")
    )
    assert body["plan"]["task"] == {
        "scope": "TASK",
        "blueprint_ref": "/tasks/1",
        "value": source.manifest["capability_blueprint"]["tasks"][1],
    }
    assert [item["value"]["key"] for item in body["plan"]["task_resources"]] == ["notes", "source"]
    assert [item["blueprint_ref"] for item in body["plan"]["task_resources"]] == [
        "/resource_requirements/1",
        "/resource_requirements/0",
    ]
    shared = body["plan"]["shared"]
    assert shared["scope"] == "SKILL"
    assert [item["value"]["key"] for item in shared["resource_requirements"]] == ["tracker"]
    assert shared["resource_requirements"][0]["scope"] == "SKILL"
    assert body["source_traces"][2]["target"] == "/tasks/0"
    assert {item["verification"] for item in body["source_traces"]} == {"TEXT_SNAPSHOT"}
    assert all("strength" not in item["value"] for item in body["plan"]["task_resources"])
    assert "edges" not in body["plan"] and "nodes" not in body["plan"]
    assert source == original


def test_checksum_covers_only_precise_public_identity_and_semantics() -> None:
    """全 hash を別計算し、readiness や実行 state が混入しないことを示す。"""

    source = _source()
    body = _project(source).to_json()
    assert body["blueprint_checksum"] == _hash(source.manifest["capability_blueprint"])
    expected = {key: value for key, value in body.items() if key != "preview_checksum"}
    assert body["preview_checksum"] == _hash(expected)
    assert "readiness" not in body
    other_project = _project(replace(source, project_id=uuid4()))
    assert other_project.preview_checksum != body["preview_checksum"]
    assert other_project.blueprint_checksum == body["blueprint_checksum"]
    assert _project(source).to_json() == body


def test_source_input_and_public_json_cannot_mutate_projected_content() -> None:
    """呼出側と response consumer の mutable JSON を保存済み DTO から切り離す。"""

    source = _source()
    result = _project(source)
    original = result.to_json()
    source.manifest["capability_blueprint"]["tasks"][1]["objective"] = "Changed input"
    source.source_snapshot[0]["content"] = "Changed original text"
    exposed = result.to_json()
    exposed["plan"]["shared"]["guidance"]["required_rules"].clear()
    exposed["identity"]["skill_key"] = "changed-output"
    assert result.to_json() == original
    assert "Cite original evidence" not in repr(result)
    assert "Changed original text" not in repr(source)


@pytest.mark.parametrize("declared", [False, True])
def test_missing_or_null_blueprint_is_not_declared_without_reconstructing_it(
    declared: bool,
) -> None:
    """旧欠落を正常に区別し、壊れた source 全文で未宣言表示まで阻止しない。"""

    source = _source()
    if declared:
        source.manifest["capability_blueprint"] = None
    else:
        del source.manifest["capability_blueprint"]
    source = _rehashed(replace(source, source_file_index=None, source_snapshot="not-read"))
    result = _project(source).to_json()
    assert result["status"] == "NOT_DECLARED"
    assert result["plan"] is None and result["source_traces"] == []
    assert result["blueprint_checksum"] is None
    assert result["preview_checksum"] == _hash(
        {key: value for key, value in result.items() if key != "preview_checksum"}
    )


def test_optional_fields_remain_absent_and_default_approval_is_not_written() -> None:
    """検査用 normalizer の既定値を原宣言へ混ぜない。"""

    source = _source()
    blueprint = source.manifest["capability_blueprint"]
    for key in ("execution_preferences", "assumptions", "questions", "interaction_points"):
        del blueprint[key]
    del blueprint["tasks"][1]["resource_keys"]
    del blueprint["effect_intents"][0]["approval_mode"]
    body = _project(_rehashed(source)).to_json()
    assert "resource_keys" not in body["plan"]["task"]["value"]
    assert body["plan"]["task_resources"] == []
    assert len(body["plan"]["shared"]["resource_requirements"]) == 3
    assert body["plan"]["shared"]["guidance"] == blueprint["guidance"]
    assert body["plan"]["shared"]["questions"] is None
    assert body["plan"]["shared"]["execution_preferences"] is None
    assert "approval_mode" not in body["plan"]["shared"]["effect_intents"][0]


def test_empty_collections_are_distinct_from_undeclared_ones() -> None:
    """明示空配列や空 object を null に畳み込まない。"""

    source = _source()
    blueprint = source.manifest["capability_blueprint"]
    blueprint["questions"] = []
    blueprint["execution_preferences"] = {}
    body = _project(_rehashed(source)).to_json()
    assert body["plan"]["shared"]["questions"] == []
    assert body["plan"]["shared"]["execution_preferences"] == {}


def test_current_runtime_default_capability_alias_is_accepted_without_rewriting() -> None:
    """既存の platform alias だけを認め、表示は原 Blueprint capability を保持する。"""

    source = _source()
    source.manifest["tasks"][1]["capability"] = "reading-guide.explain"
    source = _rehashed(source)
    original = deepcopy(source)
    body = _project(source).to_json()
    assert body["plan"]["task"]["value"]["capability"] == "reading.explain"
    assert source == original


def test_chinese_source_path_uses_original_ascii_escaped_index_hash() -> None:
    """原 importer hash と共通 canonical JSON の相違を意図的に固定する。"""

    source = _source()
    assert source.source_hash == _index_hash(source.source_file_index)
    assert source.source_hash != _hash(source.source_file_index)
    assert _project(source).status == "AVAILABLE"


def test_new_json_hash_cannot_replace_historical_source_index_hash() -> None:
    """新 hash を両 identity へ整合させても過去 index 形式の変更を許さない。"""

    source = _source()
    wrong_hash = _hash(source.source_file_index)
    source.manifest["identity"]["source_hash"] = wrong_hash
    source.manifest["capability_blueprint"]["identity"]["source_hash"] = wrong_hash
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(replace(source, source_hash=wrong_hash)))


@pytest.mark.parametrize("size", [0, 2_000_000])
def test_empty_and_maximum_source_text_bytes_are_verified_without_truncation(size: int) -> None:
    """ゼロ byte と import 上限ぴったりの原 byte 列を同じ入口で検証する。"""

    source = _source()
    content = "x" * size
    source.source_file_index[0].update(size=size, sha256="sha256:" + sha256_hex(content))
    source.source_snapshot[0]["content"] = content
    for trace in source.manifest["capability_blueprint"]["source_traces"]:
        trace["line"] = None
    result = _project(_rebound_index(source))
    assert {item["verification"] for item in result.source_traces} == {"TEXT_SNAPSHOT"}


@pytest.mark.parametrize("excess", [False, True])
def test_total_source_index_size_limit_is_enforced(excess: bool) -> None:
    """Blob を読み込まず index の総 byte 上限を正確に検査する。"""

    source = _source()
    template = source.source_file_index[0]
    entries = [
        {**template, "path": f"asset-{index:02}.bin", "size": 2_000_000, "binary": True}
        for index in range(10)
    ]
    if excess:
        entries.append({**template, "path": "asset-10.bin", "size": 1, "binary": True})
    for trace in source.manifest["capability_blueprint"]["source_traces"]:
        trace.update(path="asset-00.bin", line=None)
    # 原 file を置換したため、Manifest 側の出典も同じ合法な index-only file を参照させる。
    for task in source.manifest["tasks"]:
        for trace in task["contract_source_trace"]:
            trace.update(source_path="asset-00.bin", line=None)
    source = _rebound_index(replace(source, source_file_index=entries, source_snapshot=[]))
    if excess:
        with pytest.raises(TaskFlowPreviewInvalidError):
            _project(source)
    else:
        assert _project(source).status == "AVAILABLE"


def test_more_than_import_file_count_is_rejected_without_touching_storage() -> None:
    """原 importer を超える file 数を存在確認目的で storage へ読み出さない。"""

    source = _source()
    template = source.source_file_index[0]
    entries = [
        {**template, "path": f"asset-{index:04}.bin", "size": 0, "binary": True}
        for index in range(1001)
    ]
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rebound_index(replace(source, source_file_index=entries, source_snapshot=[])))


def test_index_only_trace_does_not_claim_blob_or_text_verification() -> None:
    """Binary 原索引は blob の存在まで証明せず、行番号を示さない。"""

    source = _source()
    for trace in source.manifest["capability_blueprint"]["source_traces"]:
        trace["line"] = None
    for task in source.manifest["tasks"]:
        for trace in task["contract_source_trace"]:
            trace["line"] = None
    source.source_file_index[0]["binary"] = True
    source = _rebound_index(replace(source, source_snapshot=[]))
    result = _project(source)
    assert {item["verification"] for item in result.source_traces} == {"SOURCE_INDEX"}
    assert result.source_traces[0]["line"] is None


@pytest.mark.parametrize("field", ["guidance", "resource_requirements"])
def test_missing_raw_required_blueprint_field_is_not_normalized(field: str) -> None:
    """必須 field の欠落を従来の validator 補完で復元しない。"""

    source = _source()
    del source.manifest["capability_blueprint"][field]
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


def test_nonbinary_file_without_its_text_snapshot_is_corrupt() -> None:
    """保存すべき原文の欠落を index-only の弱い成功へ落とさない。"""

    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(replace(_source(), source_snapshot=[]))


def test_binary_index_trace_with_unverifiable_line_is_rejected() -> None:
    """Binary へ表示できない行位置を付けない。"""

    source = _source()
    source.source_file_index[0]["binary"] = True
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rebound_index(replace(source, source_snapshot=[])))


def test_available_preview_requires_a_valid_original_manifest_contract() -> None:
    """Task の key/capability だけが残った損傷を発行済み語義として見せない。"""

    source = _source()
    for key in ("input_contract", "input_schema", "input_schema_checksum", "contract_source_trace"):
        del source.manifest["tasks"][1][key]
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "skill_id",
        "skill_version_id",
        "skill_source_id",
        "interpretation_id",
    ],
)
def test_nil_internal_identity_is_invalid(field: str) -> None:
    """Loader からの内部 ID でも nil を原版として信頼しない。"""

    changes: dict[str, Any] = {field: UUID(int=0)}
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(replace(_source(), **changes))


@pytest.mark.parametrize(
    "field", ["skill_key", "source_hash", "interpretation_id", "interpreter_version"]
)
@pytest.mark.parametrize("owner", ["manifest", "blueprint"])
def test_saved_identity_mismatch_is_rejected_even_with_a_matching_manifest_hash(
    field: str, owner: str
) -> None:
    """元 hash だけでは source/interpretation のすり替えを正当化できない。"""

    source = _source()
    target = source.manifest if owner == "manifest" else source.manifest["capability_blueprint"]
    target["identity"][field] = "different-original-identity"
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize("value", [False, [], "malformed", 1])
def test_declared_non_object_blueprint_is_not_treated_as_legacy(value: Any) -> None:
    """宣言済み損傷を未宣言の成功へ変換しない。"""

    source = _source()
    source.manifest["capability_blueprint"] = value
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize(
    "field",
    [
        "guidance",
        "execution_preferences",
        "resource_requirements",
        "interaction_points",
        "effect_intents",
        "assumptions",
        "questions",
    ],
)
def test_explicit_null_optional_collection_is_not_normalized_to_valid(field: str) -> None:
    """validator normalizer による null の修復を読む境界で拒否する。"""

    source = _source()
    source.manifest["capability_blueprint"][field] = None
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize(
    "pointer",
    [
        "/missing",
        "/tasks/01",
        "/tasks/-",
        "/tasks/999",
        "/tasks/\uff11",
        "/tasks/1/objective/x",
        "/tasks/~2",
        "/tasks/~",
        "tasks/1",
        "",
        "/tasks/1/absent",
    ],
)
def test_source_trace_must_resolve_exact_original_json_pointer(pointer: str) -> None:
    """曖昧な pointer と未存在 field を原文の証拠として見せない。"""

    source = _source()
    source.manifest["capability_blueprint"]["source_traces"][0]["target"] = pointer
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "a//b",
        "a/./b",
        "a/../b",
        "a\\b",
        "https://example.invalid/a",
        "a\x00b",
        "a\rb",
        "a\nb",
        "a\tb",
        "a/",
        "",
        "a\ud800b",
    ],
)
def test_unsafe_source_path_cannot_be_used_for_trace(path: str) -> None:
    """検査時に path を直して別 file の証明へ変換しない。"""

    source = _source()
    source.manifest["capability_blueprint"]["source_traces"][0]["path"] = path
    if "\ud800" in path:
        with pytest.raises(TaskFlowPreviewInvalidError):
            _project(source)
    else:
        with pytest.raises(TaskFlowPreviewInvalidError):
            _project(_rehashed(source))


@pytest.mark.parametrize("line", [0, -1, True, False, 1.0, 4, "2"])
def test_text_snapshot_line_is_strict_and_within_saved_text(line: Any) -> None:
    """CRLF 原文の実際の行だけを確認し、bool と整数を混同しない。"""

    source = _source()
    source.manifest["capability_blueprint"]["source_traces"][0]["line"] = line
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize(
    "collection",
    [
        "tasks",
        "capabilities",
        "resource_requirements",
        "interaction_points",
        "effect_intents",
        "questions",
        "assumptions",
        "required_rules",
        "recommended_steps",
        "quality_criteria",
        "prohibited_actions",
        "success_criteria",
        "deliverables",
        "session_split_hints",
        "stop_conditions",
    ],
)
def test_duplicate_keys_are_rejected_in_every_independent_collection(collection: str) -> None:
    """異種 collection の同名は許しても同じ原位置群の ID 衝突は許さない。"""

    source = _source()
    blueprint = source.manifest["capability_blueprint"]
    if collection in {
        "required_rules",
        "recommended_steps",
        "quality_criteria",
        "prohibited_actions",
    }:
        target = blueprint["guidance"][collection]
    elif collection in {"success_criteria", "deliverables"}:
        target = blueprint["tasks"][1][collection]
    elif collection in {"session_split_hints", "stop_conditions"}:
        target = [{"key": "note", "text": "Shared note"}]
        blueprint["execution_preferences"][collection] = target
    else:
        target = blueprint[collection]
    target.append(deepcopy(target[0]))
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rehashed(source))


@pytest.mark.parametrize(
    "change",
    [
        "size",
        "hash",
        "mime",
        "binary",
        "path",
        "extra",
        "duplicate",
        "missing",
        "total",
        "order",
    ],
)
def test_source_index_shape_limits_and_binding_are_checked_after_its_hash_matches(
    change: str,
) -> None:
    """一致する source hash があっても壊れた file manifest を受理しない。"""

    source = _source()
    item = source.source_file_index[0]
    if change == "size":
        item["size"] = True
    elif change == "hash":
        item["sha256"] = "sha256:short"
    elif change == "mime":
        item["mime"] = None
    elif change == "binary":
        item["binary"] = "false"
    elif change == "path":
        item["path"] = "a/../SKILL.md"
    elif change == "extra":
        item["untrusted"] = "not-a-file-field"
    elif change == "duplicate":
        source.source_file_index.append(deepcopy(item))
    elif change == "missing":
        source.source_file_index.clear()
    elif change == "total":
        item["size"] = 2_000_001
    else:
        source.source_file_index.append({**item, "path": "a.md"})
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(_rebound_index(source))


@pytest.mark.parametrize(
    "change", ["content", "missing", "unknown", "duplicate", "extra", "surrogate"]
)
def test_text_snapshot_cannot_be_coerced_or_mismatched(change: str) -> None:
    """Snapshot の本文型、欠落、未知 file と per-file bytes/hash の不一致を拒否する。"""

    source = _source()
    item = source.source_snapshot[0]
    if change == "content":
        item["content"] = "corrupted original bytes"
    elif change == "missing":
        del item["content"]
    elif change == "unknown":
        item["path"] = "unknown.md"
    elif change == "duplicate":
        source.source_snapshot.append(deepcopy(item))
    elif change == "extra":
        item["extra"] = True
    else:
        item["content"] = "\ud800"
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(source)


@pytest.mark.parametrize(
    "change", ["manifest_hash", "source_hash", "task_capability", "task_missing", "required_trace"]
)
def test_independent_binding_and_required_rule_checks_fail_closed(change: str) -> None:
    """現 publish gate だけでは拒否できない精確版・Task・必須規則の不整合も拒否する。"""

    source = _source()
    if change == "manifest_hash":
        source = replace(source, manifest_checksum="sha256:" + "0" * 64)
    elif change == "source_hash":
        source.source_file_index[0]["size"] += 1
    elif change == "task_capability":
        source.manifest["tasks"][1]["capability"] = "other.capability"
        source = _rehashed(source)
    elif change == "task_missing":
        source.manifest["capability_blueprint"]["tasks"][1]["key"] = "different"
        source = _rehashed(source)
    else:
        source.manifest["capability_blueprint"]["source_traces"].pop(1)
        source = _rehashed(source)
    with pytest.raises(TaskFlowPreviewInvalidError):
        _project(source)


@pytest.mark.parametrize("task_key", ["", "Uppercase", "../task", "a/b", "a" * 129, "\ud800"])
def test_invalid_requested_task_key_preserves_the_original_static_rejection(task_key: str) -> None:
    """共有門禁の抽出で、不正な request key を正常な不存在や別 Task へ変換しない。"""

    with pytest.raises(TaskFlowPreviewInvalidError):
        project_task_flow_preview(source=_source(), task_key=task_key, contracts_dir=CONTRACTS)


def test_unknown_manifest_task_is_not_found_instead_of_corrupt() -> None:
    """有効な精確版に要求 Task がない場合だけ not found とする。"""

    with pytest.raises(PublishedTaskNotFoundError):
        project_task_flow_preview(source=_source(), task_key="unknown", contracts_dir=CONTRACTS)


def test_invalid_error_is_static_and_does_not_chain_raw_validator_payload() -> None:
    """不正原値を含む jsonschema 診断を公開例外へ含めない。"""

    source = _source()
    source.manifest["capability_blueprint"]["tasks"][1]["objective"] = {"secret-example": "hidden"}
    with pytest.raises(TaskFlowPreviewInvalidError) as caught:
        _project(_rehashed(source))
    assert str(caught.value) == "The saved task flow preview is invalid."
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert "secret-example" not in repr(caught.value)
