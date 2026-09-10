"""原 uniqueItems の数値同値を共有 compiler で守り、合法な保存 hash を変えない。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from projectmind.core.hashing import canonical_json
from projectmind.skills.task_contract import (
    TaskContractCompilationError,
    compile_task_contract,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _draft(value_type: str, values: list[object]) -> dict[str, Any]:
    """外部入力と同じ原 scalar 表記を持つ最小 TaskContractDraft を作る。"""

    return {
        "contract_version": "projectmind.task-contract-draft/v1",
        "type": value_type,
        "enum": values,
    }


def _original_validator() -> Draft202012Validator:
    """手製の等値規則ではなく、以前からある Blueprint Schema を正本として読む。"""

    blueprint = json.loads(
        (CONTRACTS / "capability-blueprint" / "v1.schema.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator({"$ref": "#/$defs/taskContractDraft", "$defs": blueprint["$defs"]})


@pytest.mark.parametrize("values", [[1, 1.0], [1.0, 1], [0, -0.0], [-0.0, 0]])
@pytest.mark.parametrize("position", ["root", "field", "items"])
def test_json_numeric_duplicates_are_rejected_at_every_contract_position(
    values: list[object],
    position: str,
) -> None:
    """表記や入れ子位置を問わず、原 Schema が重複とする scalar を同じ code で拒否する。"""

    number = {"type": "number", "enum": values}
    draft: dict[str, Any] = {"contract_version": "projectmind.task-contract-draft/v1"}
    if position == "root":
        draft.update(number)
        expected_path = "/enum/1"
    elif position == "field":
        draft.update(type="object", fields=[{"key": "value", "required": True, **number}])
        expected_path = "/fields/0/enum/1"
    else:
        draft.update(type="array", items=number)
        expected_path = "/items/enum/1"
    assert not _original_validator().is_valid(draft)
    before = deepcopy(draft)
    with pytest.raises(TaskContractCompilationError) as captured:
        compile_task_contract(draft)
    assert captured.value.code == "contract_enum_duplicate"
    assert captured.value.path == expected_path
    assert draft == before


@pytest.mark.parametrize("integer", [2**53, 2**100])
def test_equal_large_integer_and_float_are_duplicates(integer: int) -> None:
    """大きな整数でも正確に同じ値の float なら原 uniqueItems に従って拒否する。"""

    draft = _draft("number", [integer, float(integer)])
    assert not _original_validator().is_valid(draft)
    with pytest.raises(TaskContractCompilationError) as captured:
        compile_task_contract(draft)
    assert captured.value.code == "contract_enum_duplicate"


@pytest.mark.parametrize("integer", [2**53 + 1, 2**100 + 1])
def test_distinct_large_integer_is_not_rounded_to_adjacent_float(integer: int) -> None:
    """float へ一括変換せず、丸められた隣接 float と原 bigint を別 enum 値として保つ。"""

    adjacent = float(integer)
    assert adjacent != integer
    draft = _draft("number", [integer, adjacent])
    assert _original_validator().is_valid(draft)
    compiled = compile_task_contract(draft)
    assert canonical_json(compiled.schema["enum"]) == canonical_json([integer, adjacent])
    assert type(compiled.schema["enum"][0]) is int
    assert type(compiled.schema["enum"][1]) is float


@pytest.mark.parametrize(
    ("value_type", "values"),
    [
        ("number", [1, True]),
        ("number", [True, 1]),
        ("number", [0.0, False]),
        ("integer", [1, True]),
        ("boolean", [True, 1]),
        ("boolean", [False, 0.0]),
    ],
)
def test_original_scalar_type_gate_precedes_numeric_equality(
    value_type: str,
    values: list[object],
) -> None:
    """Python の bool/int 同値に依存せず、従来どおり異なる scalar 型の混入として拒否する。"""

    with pytest.raises(TaskContractCompilationError) as captured:
        compile_task_contract(_draft(value_type, values))
    assert captured.value.code == "contract_enum_type_mismatch"


@pytest.mark.parametrize(
    ("value_type", "values", "original_checksum"),
    [
        (
            "number",
            [1, 2.0, -0.0, 9007199254740993, 9007199254740992.0],
            "sha256:f33b651e0ce975febe120c5de9c12ff371d1d74f53504e2e21827ae0396ece8f",
        ),
        (
            "integer",
            [0, 1, 9007199254740993],
            "sha256:84ee9e2e16e3fa53c9ae5222059c5b4ca3aeb27317b22dcebf304fa42c89c832",
        ),
        (
            "boolean",
            [False, True],
            "sha256:0af617a6e340c19f4a26cd3e1a4c7ef4563401341a24977550a0b01575703351",
        ),
        (
            "string",
            ["1", "1.0", "中文"],
            "sha256:5dbce1edb696435c1aae015846e115d8b102f4c3cb934af092724fdb8537b431",
        ),
    ],
)
def test_valid_enum_keeps_original_order_representation_and_checksum(
    value_type: str,
    values: list[object],
    original_checksum: str,
) -> None:
    """修正前 compiler で採取した既知 checksum と比較し、合法な原 JSON を書き換えない。"""

    draft = _draft(value_type, values)
    before = canonical_json(draft)
    assert _original_validator().is_valid(draft)
    compiled = compile_task_contract(draft)
    assert compiled.checksum == original_checksum
    assert canonical_json(compiled.schema["enum"]) == canonical_json(values)
    assert [type(item) for item in compiled.schema["enum"]] == [type(item) for item in values]
    assert canonical_json(draft) == before


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_enum_still_fails_json_boundary(value: float) -> None:
    """同値判定の修正で、既存 canonical JSON の非有限値拒否を失わない。"""

    with pytest.raises(ValueError):
        compile_task_contract(_draft("number", [value]))
