"""CapabilityBlueprint の決定的検証と effect 境界を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from projectmind.skills.capability_blueprint import (
    CAPABILITY_BLUEPRINT_VERSION,
    CapabilityBlueprintError,
    CapabilityBlueprintValidator,
    normalize_capability_blueprint,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


@pytest.fixture(name="validator")
def _validator() -> CapabilityBlueprintValidator:
    """凍結済み contract を読み込んだ validator を共有する。"""

    return CapabilityBlueprintValidator(CONTRACTS)


def _example() -> dict[str, Any]:
    """契約 example を各テストが安全に改変できるよう複製して返す。"""

    return json.loads((CONTRACTS / "examples/capability-blueprint.v1.json").read_text("utf-8"))


def _minimal_blueprint() -> dict[str, Any]:
    """資源も効果も持たない最小の guidance 専用 blueprint を返す。"""

    return {
        "blueprint_version": CAPABILITY_BLUEPRINT_VERSION,
        "identity": {
            "skill_key": "minimal-skill",
            "source_hash": "sha256:" + "b2" * 32,
            "interpretation_id": "00000000-0000-4000-8000-00000000b201",
            "interpreter_version": "projectmind-skill-interpreter/2.1.0",
        },
        "compatibility": {"level": "assisted"},
        "capabilities": [{"key": "advice.give", "title": "Give Advice"}],
        "tasks": [
            {
                "key": "advise",
                "capability": "advice.give",
                "objective": "Advise the user using the guidance carried by the Skill.",
            }
        ],
        "source_traces": [
            {
                "target": "/tasks/0",
                "path": "SKILL.md",
                "line": 2,
                "reason": "The Overview section states the advisory goal.",
            }
        ],
    }


def test_contract_example_validates_and_is_checksum_stable(
    validator: CapabilityBlueprintValidator,
) -> None:
    """公開 example が契約に適合し、同じ入力から同じ checksum を得る。"""

    first = validator.validate(_example())
    second = validator.validate(_example())

    assert first.checksum == second.checksum
    assert first.checksum.startswith("sha256:")
    assert first.blueprint["blueprint_version"] == CAPABILITY_BLUEPRINT_VERSION


def test_minimal_blueprint_is_completed_without_business_schema(
    validator: CapabilityBlueprintValidator,
) -> None:
    """業務 Schema も資源も無い Skill が空集合の補完だけで有効な蓝図になる。"""

    compiled = validator.validate(_minimal_blueprint())

    assert compiled.blueprint["resource_requirements"] == []
    assert compiled.blueprint["effect_intents"] == []
    assert compiled.blueprint["guidance"]["required_rules"] == []
    assert compiled.blueprint["tasks"][0].get("result_contract") is None


def test_unknown_domain_capability_is_accepted(
    validator: CapabilityBlueprintValidator,
) -> None:
    """Tool catalog に存在しない領域 capability だけでは失敗しない。

    docs/05 §8.2 のとおり、未知の業務能力は就緒度の問題であり発行 gate ではない。
    """

    blueprint = _minimal_blueprint()
    blueprint["capabilities"] = [
        {"key": "projectmind.development.readiness", "title": "Development Readiness"}
    ]
    blueprint["tasks"][0]["capability"] = "projectmind.development.readiness"

    compiled = validator.validate(blueprint)

    assert compiled.blueprint["capabilities"][0]["key"] == "projectmind.development.readiness"


def test_recommended_profile_is_not_invented_by_normalization() -> None:
    """Skill が推奨しない自主レベルを正規化が捏造しない。

    recommended_profile は Skill の推奨であり、既定値は ExecutionProfilePolicy が決める。
    """

    normalized = normalize_capability_blueprint(_minimal_blueprint())

    assert "recommended_profile" not in normalized["execution_preferences"]
    assert normalized["execution_preferences"]["stop_conditions"] == []


def test_apply_intent_defaults_to_ask_approval() -> None:
    """apply の承認 mode 省略が「承認不要」と解釈されない。"""

    blueprint = _example()
    for intent in blueprint["effect_intents"]:
        intent.pop("approval_mode", None)

    normalized = normalize_capability_blueprint(blueprint)

    modes = {item["key"]: item.get("approval_mode") for item in normalized["effect_intents"]}
    assert modes["update-tracker"] == "ask"
    assert modes["read-repository"] is None


def test_apply_intent_without_resource_is_rejected(
    validator: CapabilityBlueprintValidator,
) -> None:
    """資源を特定しない apply 意図は Project が scope を固定できないため拒否する。"""

    blueprint = _example()
    for intent in blueprint["effect_intents"]:
        if intent["mode"] == "apply":
            del intent["resource_key"]

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "effect_resource_missing"
    assert error.value.path == "/effect_intents/1/resource_key"


def test_apply_intent_on_read_resource_is_rejected(
    validator: CapabilityBlueprintValidator,
) -> None:
    """read 資源に対する apply 宣言は矛盾として拒否する。"""

    blueprint = _example()
    for resource in blueprint["resource_requirements"]:
        if resource["key"] == "review_tracker":
            resource["access"] = "read"

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "effect_resource_not_writable"


def test_apply_intent_cannot_declare_preauthorized_approval(
    validator: CapabilityBlueprintValidator,
) -> None:
    """Skill 側が事前承認を宣言して ask を外すことはできない。

    事前許可は ADMIN の Project 設定であり、Skill の申告で権限にはならない。
    """

    blueprint = _example()
    for intent in blueprint["effect_intents"]:
        if intent["mode"] == "apply":
            intent["approval_mode"] = "preauthorized"

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "blueprint_schema_invalid"
    assert error.value.path == "/effect_intents/1/approval_mode"


def test_observe_intent_must_not_carry_approval_mode(
    validator: CapabilityBlueprintValidator,
) -> None:
    """承認を持たない mode に approval_mode を残さない。"""

    blueprint = _example()
    for intent in blueprint["effect_intents"]:
        if intent["mode"] == "observe":
            intent["approval_mode"] = "ask"

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "effect_approval_mode_inapplicable"


def test_required_rule_without_source_trace_is_rejected(
    validator: CapabilityBlueprintValidator,
) -> None:
    """根拠 trace の無い required rule は Interpreter の推測なので拒否する。"""

    blueprint = _example()
    blueprint["guidance"]["required_rules"].append(
        {"key": "invented-rule", "text": "Always escalate to the release manager."}
    )

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "required_rule_source_trace_missing"
    assert error.value.path == "/guidance/required_rules/1"


def test_recommended_step_without_source_trace_is_accepted(
    validator: CapabilityBlueprintValidator,
) -> None:
    """推奨手順は Agent が再構成できるため trace 必須にしない。"""

    blueprint = _example()
    blueprint["guidance"]["recommended_steps"].append(
        {"key": "extra-step", "text": "Skim the changelog before reading the diff."}
    )

    compiled = validator.validate(blueprint)

    assert len(compiled.blueprint["guidance"]["recommended_steps"]) == 3


def test_task_resource_key_must_be_declared(
    validator: CapabilityBlueprintValidator,
) -> None:
    """Task が未宣言の資源 key を参照したまま凍結されない。"""

    blueprint = _example()
    blueprint["tasks"][0]["resource_keys"].append("undeclared_resource")

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "task_resource_unknown"


def test_task_capability_must_be_declared(
    validator: CapabilityBlueprintValidator,
) -> None:
    """Task が capabilities に無い能力を指したまま凍結されない。"""

    blueprint = _example()
    blueprint["tasks"][0]["capability"] = "repository.audit"

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "task_capability_unknown"
    assert error.value.path == "/tasks/0/capability"


def test_duplicate_resource_key_is_rejected(
    validator: CapabilityBlueprintValidator,
) -> None:
    """同じ資源 key の重複は束縛先を曖昧にするため拒否する。"""

    blueprint = _example()
    blueprint["resource_requirements"].append(
        deepcopy(blueprint["resource_requirements"][0])
    )

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "resource_key_duplicate"


def test_uncompilable_result_contract_is_rejected(
    validator: CapabilityBlueprintValidator,
) -> None:
    """発行後ではなく蓝図検証時に壊れた任意 contract を検出する。"""

    blueprint = _example()
    blueprint["tasks"][0]["result_contract"]["fields"][0]["type"] = "object"

    with pytest.raises(CapabilityBlueprintError) as error:
        validator.validate(blueprint)

    assert error.value.code == "result_contract_invalid"
    assert error.value.path.startswith("/tasks/0/result_contract")


def test_source_trace_order_does_not_change_checksum(
    validator: CapabilityBlueprintValidator,
) -> None:
    """Trace の出力順ゆれが実行契約の identity を変えない。"""

    blueprint = _example()
    reversed_traces = _example()
    reversed_traces["source_traces"] = list(reversed(reversed_traces["source_traces"]))

    assert validator.validate(blueprint).checksum == validator.validate(reversed_traces).checksum


def test_capability_order_changes_checksum(
    validator: CapabilityBlueprintValidator,
) -> None:
    """Trace の pointer が指す配列は並べ替えず、内容差を checksum へ反映する。"""

    blueprint = _example()
    extended = _example()
    extended["capabilities"].append({"key": "repository.audit", "title": "Repository Audit"})

    assert validator.validate(blueprint).checksum != validator.validate(extended).checksum
