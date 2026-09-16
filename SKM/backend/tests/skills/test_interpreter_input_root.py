"""新候補の入力 root だけを Run API の object 契約へ一致させる。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def candidate() -> dict[str, Any]:
    """外部データを含まない、入力を要求しない native v2 候補を作る。"""

    return {
        "candidate_version": "skillmind.skill-candidate/v2",
        "title": None,
        "description": None,
        "input_contract": {
            "contract_version": "skillmind.task-contract-draft/v1",
            **node("object"),
            "fields": [],
        },
        "input_source_ref": "s0",
        "resource_requirements": [],
        "platform_tools": [],
        "diagnostics": [],
    }


def node(kind: str) -> dict[str, Any]:
    """省略を null とする wire 表現を明示し、業務 null とは混同しない。"""

    return {
        "type": kind, "description": None, "enum": None, "fields": None, "items": None,
        "min_length": None, "max_length": None, "pattern": None,
        "minimum": None, "maximum": None,
    }


def validator() -> Draft202012Validator:
    """本番と同じ Schema file を読み、テスト専用 Schema を使わない。"""

    schema = json.loads(
        (CONTRACTS / "skills/interpreter/v2/candidate.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("kind", ["array", "string", "integer", "number", "boolean"])
def test_scalar_and_array_input_roots_are_rejected(kind: str) -> None:
    """API から呼べない入力 root はモデル候補時点で拒否する。"""

    value = candidate()
    value["input_contract"]["type"] = kind
    with pytest.raises(ValidationError):
        validator().validate(value)


def test_empty_object_input_is_valid() -> None:
    """呼出し引数なしの Skill は空 object 契約のまま通す。"""

    validator().validate(candidate())


@pytest.mark.parametrize("kind", ["object", "array", "string", "integer", "number", "boolean"])
def test_nested_input_types_remain_available(kind: str) -> None:
    """object root の制約を nested field と array items へ波及させない。"""

    value = candidate()
    field = {"key": "value", "required": False, **node(kind)}
    if kind == "array":
        field["items"] = node("string")
    if kind == "object":
        field["fields"] = []
    value["input_contract"]["fields"] = [field]
    validator().validate(value)


@pytest.mark.parametrize("kind", ["array", "string", "integer", "number", "boolean"])
def test_compiler_independently_rejects_invalid_input_root(kind: str) -> None:
    """Schema の呼び出しを迂回しても、編訳器は呼出し不能な root を受理しない。"""

    from skillmind.skills.capability_blueprint import CapabilityBlueprintError
    from skillmind.skills.direct_candidate import compile_candidate

    value = candidate()
    value["input_contract"]["type"] = kind
    with pytest.raises(CapabilityBlueprintError) as caught:
        compile_candidate({}, value, CONTRACTS)
    assert caught.value.code == "candidate_input_root_invalid"
    assert caught.value.path == "/input_contract/type"
