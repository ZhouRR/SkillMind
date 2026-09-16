"""候補の許容形状と実際の編訳条件を揃え、出典エラーを元 field に限定する。"""

from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from skillmind.skills.candidate_revision import repair_components, validate_repair_scope
from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.direct_candidate import candidate_schema, compile_candidate
from skillmind.skills.source_projection import SourceLocations, omit_optional_nulls
from tests.skills.test_skill_service import CONTRACTS
from tests.skills.test_source_execution import direct_case


@pytest.mark.parametrize("capabilities", [None, []])
def test_resource_capabilities_cannot_request_an_uncompilable_shape(capabilities) -> None:
    """必須の能力を null で生成できる Schema と後段拒否の矛盾を除く。"""
    _, candidate = direct_case(writes=True)
    candidate["resource_requirements"][0]["capabilities"] = capabilities
    errors = list(Draft202012Validator(candidate_schema(CONTRACTS)).iter_errors(candidate))
    assert any(
        list(error.absolute_path)
        == [
            "resource_requirements",
            0,
            "capabilities",
        ]
        for error in errors
    )


@pytest.mark.parametrize(
    "component,pointer",
    [
        ("input_source_ref", "/input_source_ref"),
        ("resource_requirements", "/resource_requirements/0/source_ref"),
        ("diagnostics", "/diagnostics/0/source_ref"),
    ],
)
def test_source_errors_identify_the_only_repairable_component(component, pointer) -> None:
    """不正 source ID を根全体の再生成理由にせず、関係ない宣言を保護する。"""
    request, candidate = direct_case(writes=True)
    if component == "input_source_ref":
        candidate[component] = "s99999"
    elif component == "resource_requirements":
        candidate[component][0]["source_ref"] = "s99999"
    else:
        candidate[component] = [
            {
                "severity": "warning",
                "code": "fixture",
                "message": "Fixture diagnostic",
                "source_ref": "s99999",
            }
        ]
    original = deepcopy(candidate)
    with pytest.raises(CapabilityBlueprintError) as caught:
        compile_candidate(request, candidate, CONTRACTS)
    error = caught.value
    assert error.path == pointer
    assert candidate == original
    fields = repair_components(error.path, error.code)
    assert fields == frozenset({component})
    repaired = deepcopy(candidate)
    repaired["title"] = "unrelated edit"
    with pytest.raises(ValueError, match="unrelated"):
        validate_repair_scope(candidate, repaired, fields)


def test_malformed_diagnostic_source_does_not_echo_model_text() -> None:
    """JSON Schema が string を許す診断出典も、安全な固定 code/path で拒否する。"""
    request, candidate = direct_case()
    candidate["diagnostics"] = [
        {
            "severity": "warning",
            "code": "fixture",
            "message": "Fixture",
            "source_ref": "s0:not-a-line-do-not-echo",
        }
    ]
    with pytest.raises(CapabilityBlueprintError) as caught:
        compile_candidate(request, candidate, CONTRACTS)
    assert caught.value.path == "/diagnostics/0/source_ref"
    assert "not-a-line-do-not-echo" not in str(caught.value)


def test_source_locations_preserve_binary_file_references_and_optional_nulls() -> None:
    """二進附件の存在は参照できるが、架空の行番号や配列値の削除は許可しない。"""
    locations = SourceLocations(
        [
            {"id": "s0", "path": "SKILL.md", "content": "first\nsecond\n"},
            {"id": "s1", "path": "asset.bin", "content": None},
        ]
    )
    assert locations.resolve("s0:2", "/input_source_ref") == {"path": "SKILL.md", "line": 2}
    assert locations.resolve("s1", "/diagnostics/0/source_ref") == {
        "path": "asset.bin",
        "line": None,
    }
    assert locations.resolve(None, "/diagnostics/0/source_ref") == {"path": None, "line": None}
    with pytest.raises(ValueError):
        locations.resolve("s1:1", "/diagnostics/0/source_ref")
    original = {"optional": None, "values": [None, {"number": 0, "flag": False}]}
    assert omit_optional_nulls(original) == {"values": [None, {"number": 0, "flag": False}]}
    assert "optional" in original
