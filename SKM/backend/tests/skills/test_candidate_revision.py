"""調整基準の凍結と、明示 scope／有界修復の非対象値保護を検証する。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from skillmind.skills.candidate_revision import (
    CandidateRevisionError,
    launch_contract_checksum,
    repair_components,
    snapshot_launch_contract,
    validate_adjustment_scope,
    validate_editable_paths,
    validate_repair_scope,
)


def manifest() -> dict[str, Any]:
    """合成された最小準備宣言。原文や資格情報を fixture に保存しない。"""

    return {
        "skill_execution": {
            "execution_version": "skillmind.skill-execution/v1",
            "tasks": [{"key": "execute", "title": "分析", "description": "入力の分析"}],
            "resource_requirements": [{"key": "reference", "required": False}],
            "source_traces": [{"target": "/resource_requirements/0", "path": "SKILL.md", "line": 1}],
        },
        "tasks": [{
            "input_contract": {
                "contract_version": "skillmind.task-contract-draft/v1", "type": "object",
                "fields": [{"key": "language", "type": "string", "required": True,
                            "enum": ["日本語", "中文"]}],
            },
            "contract_source_trace": [{"contract": "input", "field_path": "/",
                                       "source_path": "SKILL.md", "line": 1}],
            "input_schema": {"type": "object"},
        }],
        "tools": [{"capability": "workspace.read/v1", "required": True}],
        "compatibility": {"level": "adapted", "diagnostics": []},
        "source_documents": [{"path": "SKILL.md", "content": "Not a second instruction copy"}],
    }


def parent(value: dict[str, Any]) -> dict[str, Any]:
    """実装の投影と checksum をそのまま凍結要求へ保持する。"""

    declaration = snapshot_launch_contract(value)
    assert declaration is not None
    return {"launch_contract": declaration,
            "launch_contract_checksum": launch_contract_checksum(declaration)}


def test_parent_projection_is_complete_defensive_and_not_raw_candidate() -> None:
    """公開された宣言だけを投影し、原文・生成 Schema・隠れたモデル出力を捏造しない。"""

    value = manifest()
    frozen = parent(value)
    assert set(frozen["launch_contract"]) == {
        "title", "description", "input_contract", "input_source_trace",
        "resource_requirements", "resource_source_traces", "tools", "diagnostics",
    }
    value["tasks"][0]["input_contract"]["fields"][0]["required"] = False
    assert frozen["launch_contract"]["input_contract"]["fields"][0]["required"] is True
    assert snapshot_launch_contract({"capability_blueprint": {}}) is None


def test_single_field_adjustment_preserves_all_other_declarations() -> None:
    """必須性一箇所だけの変更は通し、元の宣言は変更しない。"""

    value = manifest()
    frozen = parent(value)
    value["tasks"][0]["input_contract"]["fields"][0]["required"] = False
    validate_adjustment_scope(frozen, {
        "editable_paths": ["/input_contract/fields/0/required"],
    }, value)
    assert frozen["launch_contract"]["input_contract"]["fields"][0]["required"] is True


@pytest.mark.parametrize("target", ["resource", "tool", "field", "delete", "diagnostic"])
def test_unrequested_declarations_cannot_drift(target: str) -> None:
    """追加・削除を含む非対象変更を、値を公開せず失敗にする。"""

    value = manifest()
    frozen = parent(value)
    if target == "resource":
        value["skill_execution"]["resource_requirements"][0]["required"] = True
    elif target == "tool":
        value["tools"].append({"capability": "workspace.write/v2", "required": True})
    elif target == "field":
        value["tasks"][0]["input_contract"]["fields"][0]["enum"] = ["other"]
    elif target == "delete":
        value["tasks"][0]["input_contract"]["fields"] = []
    else:
        value["compatibility"]["diagnostics"] = [{"message": "changed"}]
    with pytest.raises(CandidateRevisionError) as caught:
        validate_adjustment_scope(frozen, {
            "editable_paths": ["/input_contract/fields/0/required"],
        }, value)
    assert caught.value.code == "candidate_adjustment_scope_exceeded"
    assert "other" not in str(caught.value)


def test_pointer_prefix_is_not_an_authorization_prefix() -> None:
    """field 1 の許可を field 10 に流用しない。"""

    value = manifest()
    fields = value["tasks"][0]["input_contract"]["fields"]
    fields.extend({"key": f"value_{i}", "required": True} for i in range(1, 11))
    frozen = parent(value)
    fields[10]["required"] = False
    with pytest.raises(CandidateRevisionError):
        validate_adjustment_scope(frozen, {"editable_paths": ["/input_contract/fields/1"]}, value)


@pytest.mark.parametrize("paths", [[], ["/"], ["/identity"], ["/input_contract//type"],
                                  ["/input_contract/a~2b"], ["/tools", "/tools"],
                                  " /tools", [3]])
def test_editable_paths_are_explicit_bounded_pointers(paths: Any) -> None:
    """曖昧な範囲や不正 escape を拒否し、自然言語から scope を推定しない。"""

    with pytest.raises(ValueError):
        validate_editable_paths(paths)


def test_parent_checksum_and_legacy_boundary_are_checked() -> None:
    """破損した基準や旧 Blueprint を scoped source 宣言として扱わない。"""

    value = manifest()
    frozen = parent(value)
    frozen["launch_contract"]["title"] = "changed"
    for baseline in (frozen, None, {"summary": "legacy"}):
        with pytest.raises(CandidateRevisionError):
            validate_adjustment_scope(baseline, {"editable_paths": ["/title"]}, value)
    validate_adjustment_scope(None, {"instruction": "ordinary legacy adjustment"}, value)


def test_repair_preserves_components_outside_the_validation_error() -> None:
    """入力の修復で資源や Tool を変更・削除させず、省略と null も区別する。"""

    before = {"input_contract": {"type": "string"}, "title": None,
              "resource_requirements": [], "platform_tools": []}
    after = deepcopy(before)
    after["input_contract"] = {"type": "object"}
    allowed = repair_components("/input_contract/type", "candidate_input_root_invalid")
    validate_repair_scope(before, after, allowed)
    after["platform_tools"] = ["workspace.write/v2"]
    with pytest.raises(CandidateRevisionError):
        validate_repair_scope(before, after, allowed)
    after = deepcopy(before)
    del after["title"]
    with pytest.raises(CandidateRevisionError):
        validate_repair_scope(before, after, allowed)


@pytest.mark.parametrize(("path", "code", "expected"), [
    ("/skill_execution/resource_requirements/0", "skill_execution_invalid", {"resource_requirements"}),
    ("/fields/0/type", "contract_type_invalid", {"input_contract"}),
    ("/platform_tools", "skill_execution_invalid", {"platform_tools"}),
    ("/tasks/0", "candidate_publish_invalid", None),
    ("/source_ref", "candidate_source_invalid", None),
])
def test_ambiguous_repair_scope_is_not_claimed_to_be_local(
    path: str, code: str, expected: set[str] | None,
) -> None:
    """複数原因・位置不明では局部修復保証を付けず、既存の全候補検証へ渡す。"""

    assert repair_components(path, code) == expected
