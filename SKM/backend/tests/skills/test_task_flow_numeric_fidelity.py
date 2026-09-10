"""実 preview API wire の原数値・型・hash を、ブラウザの Number 変換前で検証する。"""

from __future__ import annotations

import json
import math
import sys
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from skillmind.api.routes.task_flow import FlowContractResponse
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.skills.task_contract import TaskContractCompilationError, compile_task_contract
from skillmind.skills.task_flow_preview import (
    TaskFlowPreviewInvalidError,
    TaskFlowPreviewSource,
    project_task_flow_preview,
)
from tests.skills.task_flow_numeric_fixtures import (
    CONTRACTS,
    make_numeric_flow_source,
    numeric_flow_wire,
)


def _with_contract(
    source: TaskFlowPreviewSource, contract: dict[str, Any]
) -> TaskFlowPreviewSource:
    """合成版の両原契約と hash を更新し、保存済み production データには触れない。"""

    manifest = deepcopy(source.manifest)
    task = manifest["capability_blueprint"]["tasks"][1]
    task["parameter_contract"] = deepcopy(contract)
    task["result_contract"] = deepcopy(contract)
    return replace(
        source,
        manifest=manifest,
        manifest_checksum="sha256:" + sha256_hex(canonical_json(manifest)),
    )


def test_real_numeric_wire_preserves_both_contracts_and_all_original_hashes() -> None:
    """実 projector/route/serializer 往復で値・int/float 表記・原 hash のいずれも変えない。"""

    source = make_numeric_flow_source()
    before = canonical_json(source.manifest)
    preview = project_task_flow_preview(source=source, task_key="explain", contracts_dir=CONTRACTS)
    wire = numeric_flow_wire(source)
    decoded = json.loads(wire)
    task = source.manifest["capability_blueprint"]["tasks"][1]
    public_task = decoded["plan"]["task"]["value"]
    for name in ("parameter_contract", "result_contract"):
        assert canonical_json(public_task[name]) == canonical_json(task[name])
    assert canonical_json(source.manifest) == before
    assert decoded["identity"]["manifest_checksum"] == source.manifest_checksum
    assert decoded["blueprint_checksum"] == preview.blueprint_checksum
    assert decoded["preview_checksum"] == preview.preview_checksum
    public_plan = {key: value for key, value in decoded.items() if key != "readiness"}
    assert canonical_json(public_plan) == canonical_json(preview.to_json())
    expected_hash = "sha256:" + sha256_hex(
        canonical_json(
            {key: value for key, value in public_plan.items() if key != "preview_checksum"}
        )
    )
    assert decoded["preview_checksum"] == expected_hash
    assert str(10**200).encode() in wire
    assert b"9007199254740992.0" in wire
    assert b"1e+100" in wire
    assert b"-0.0" in wire
    assert b"1.7976931348623157e+308" in wire
    assert b"5e-324" in wire


@pytest.mark.parametrize("contract_name", ["parameter_contract", "result_contract"])
def test_mixed_numeric_enum_requires_python_int_and_binary64_value_semantics(
    contract_name: str,
) -> None:
    """十進 token の同値化でも全 Number 化でも、異なる原 enum を同一にしない。"""

    decoded = json.loads(numeric_flow_wire())
    fields = decoded["plan"]["task"]["value"][contract_name]["fields"]
    large = fields[0]["enum"]
    assert large[:2] == [9007199254740992, 9007199254740993]
    assert all(type(value) is int for value in large)
    assert large[0] != large[1]
    assert float(large[0]) == float(large[1])
    mixed = fields[1]["enum"]
    assert [type(value) for value in mixed] == [int, float, int, float]
    assert mixed[0] != mixed[1]
    assert mixed[2] == 10**100 and mixed[3] == 1e100
    assert mixed[2] != mixed[3]
    assert int(mixed[3]) == (
        10000000000000000159028911097599180468360808563945281389781327557747838772170381060813469985856815104
    )
    # wire の指数 token を無限精度 decimal の 10**100 に置換すると、原 enum の意味を変える。
    compile_task_contract(
        {
            "contract_version": "skillmind.task-contract-draft/v1",
            "type": "number",
            "enum": mixed,
        }
    )


@pytest.mark.parametrize("contract_name", ["parameter_contract", "result_contract"])
def test_float_extremes_negative_zero_and_nested_values_survive_wire(contract_name: str) -> None:
    """有限最大値・最小 subnormal・負零と再帰値を、表示前に欠落や正規化しない。"""

    decoded = json.loads(numeric_flow_wire())
    fields = decoded["plan"]["task"]["value"][contract_name]["fields"]
    extremes = fields[2]
    assert extremes["minimum"] == -sys.float_info.max
    assert extremes["maximum"] == sys.float_info.max
    assert extremes["enum"] == [-sys.float_info.max, -0.0, 5e-324, sys.float_info.max]
    assert all(type(value) is float for value in extremes["enum"])
    assert math.copysign(1.0, extremes["enum"][1]) == -1.0
    nested = fields[3]["items"]["fields"][0]
    assert nested["maximum"] == 10**200
    assert nested["enum"] == [9007199254740993, 1.0, 1e100, 10**200]
    assert [type(value) for value in nested["enum"]] == [int, float, float, int]


@pytest.mark.parametrize("power", [16, 100, 512, 2000])
def test_large_integer_wire_has_no_binary64_rounding(power: int) -> None:
    """Python が受理する大整数は token 長を縮めず、原 enum と上下限を往復させる。"""

    integer = 10**power + 1
    contract = {
        "contract_version": "skillmind.task-contract-draft/v1",
        "type": "integer",
        "minimum": -integer,
        "maximum": integer,
        "enum": [-integer, integer],
    }
    wire = numeric_flow_wire(_with_contract(make_numeric_flow_source(), contract))
    decoded = json.loads(wire)
    for name in ("parameter_contract", "result_contract"):
        saved = decoded["plan"]["task"]["value"][name]
        assert canonical_json(saved) == canonical_json(contract)
        assert type(saved["maximum"]) is int
    assert str(integer).encode() in wire


@pytest.mark.parametrize("values", [[1, 1.0], [0, -0.0]])
@pytest.mark.parametrize("nested", [False, True])
def test_equal_integer_float_values_do_not_become_legal_by_wire_spelling(
    values: list[int | float],
    nested: bool,
) -> None:
    """int/float token の別字面だけを根拠に、共有 compiler の元の重複拒否を外さない。"""

    contract: dict[str, Any] = {
        "contract_version": "skillmind.task-contract-draft/v1",
        "type": "number",
        "enum": values,
    }
    if nested:
        contract = {
            "contract_version": "skillmind.task-contract-draft/v1",
            "type": "object",
            "fields": [
                {
                    "key": "value",
                    "type": "array",
                    "required": True,
                    "items": {"type": "number", "enum": values},
                }
            ],
        }
    with pytest.raises(TaskContractCompilationError) as captured:
        compile_task_contract(json.loads(canonical_json(contract)))
    assert captured.value.code == "contract_enum_duplicate"


def test_source_byte_limits_do_not_bound_legal_flow_response_size() -> None:
    """短い source でも原 enum string は別の生成物なので、1 MiB 超の合法応答を切り捨てない。"""

    source = make_numeric_flow_source()
    assert sum(entry["size"] for entry in source.source_file_index) < 1024
    text = "x" * (1_048_576 + 1)
    contract = {
        "contract_version": "skillmind.task-contract-draft/v1",
        "type": "string",
        "enum": [text],
    }
    wire = numeric_flow_wire(_with_contract(source, contract))
    assert len(wire) > 2 * 1_048_576
    decoded = json.loads(wire)
    for name in ("parameter_contract", "result_contract"):
        assert decoded["plan"]["task"]["value"][name]["enum"] == [text]


def _positioned_contract(node: dict[str, Any], position: str) -> dict[str, Any]:
    """数値宣言の意味を変えず、root/field/items の原再帰境界へ配置する。"""

    result: dict[str, Any] = {"contract_version": "skillmind.task-contract-draft/v1"}
    if position == "root":
        result.update(node)
    elif position == "field":
        result.update(type="object", fields=[{"key": "value", "required": True, **node}])
    else:
        result.update(type="array", items=node)
    return result


@pytest.mark.parametrize("position", ["root", "field", "items"])
@pytest.mark.parametrize(
    ("minimum", "maximum", "valid"),
    [
        (9007199254740992.0, 9007199254740993, True),
        (9007199254740993, 9007199254740992.0, False),
        (10**100, 1e100, True),
        (1e100, 10**100, False),
        (-1e100, -(10**100), True),
        (-(10**100), -1e100, False),
        (-9007199254740993, -9007199254740992.0, True),
        (-9007199254740992.0, -9007199254740993, False),
        (-0.0, 0, True),
        (1, 1.0, True),
        (0, 5e-324, True),
        (5e-324, 0, False),
        (sys.float_info.max, 10**309, True),
        (10**309, sys.float_info.max, False),
    ],
)
def test_numeric_bounds_use_exact_integer_and_binary64_order_at_every_depth(
    position: str,
    minimum: int | float,
    maximum: int | float,
    valid: bool,
) -> None:
    """Number 化や十進指数の置換ではなく、原 compiler と API の上下限判定を保つ。"""

    contract = _positioned_contract(
        {"type": "number", "minimum": minimum, "maximum": maximum}, position
    )
    before = canonical_json(contract)
    source = _with_contract(make_numeric_flow_source(), contract)
    if not valid:
        with pytest.raises(TaskContractCompilationError) as captured:
            compile_task_contract(contract)
        assert captured.value.code == "contract_number_range_invalid"
        with pytest.raises(ValidationError):
            FlowContractResponse.model_validate_json(before)
        with pytest.raises(TaskFlowPreviewInvalidError):
            project_task_flow_preview(source=source, task_key="explain", contracts_dir=CONTRACTS)
    else:
        compile_task_contract(contract)
        model = FlowContractResponse.model_validate_json(before)
        assert canonical_json(model.model_dump(mode="json", exclude_unset=True)) == before
        decoded = json.loads(numeric_flow_wire(source))
        for name in ("parameter_contract", "result_contract"):
            assert canonical_json(decoded["plan"]["task"]["value"][name]) == before
    assert canonical_json(contract) == before


@pytest.mark.parametrize("position", ["root", "field", "items"])
def test_integer_contract_accepts_float_bounds_without_promoting_float_enum(position: str) -> None:
    """integer の上下限は number だが、enum は原 int token だけという既存規則を区別する。"""

    node = {"type": "integer", "minimum": 0.5, "maximum": 2.0, "enum": [1, 2]}
    contract = _positioned_contract(node, position)
    decoded = json.loads(numeric_flow_wire(_with_contract(make_numeric_flow_source(), contract)))
    for name in ("parameter_contract", "result_contract"):
        assert canonical_json(decoded["plan"]["task"]["value"][name]) == canonical_json(contract)
    invalid = _positioned_contract({**node, "enum": [1.0]}, position)
    with pytest.raises(TaskContractCompilationError) as captured:
        compile_task_contract(invalid)
    assert captured.value.code == "contract_enum_type_mismatch"
    with pytest.raises(ValidationError):
        FlowContractResponse.model_validate_json(canonical_json(invalid))
