"""前置効果の source 根拠・参照・旧 Worker 門禁を blueprint/manifest の境界で検証する。"""

from __future__ import annotations

from copy import deepcopy

import pytest

from skillmind.skills.capability_blueprint import (
    CapabilityBlueprintError,
    CapabilityBlueprintValidator,
)
from skillmind.skills.document_prerequisites import document_prerequisites
from tests.skills.test_capability_blueprint import CONTRACTS, _minimal_blueprint


def _blueprint():
    """業務 table/列を共通 protocol に埋め込まない、原文依拠の apply 前置条件。"""
    blueprint = _minimal_blueprint()
    blueprint["tasks"][0]["document_prerequisites"] = ["register-run"]
    blueprint["resource_requirements"] = [
        {"key": "records", "kind": "other", "access": "write", "required": True}
    ]
    blueprint["effect_intents"] = [
        {
            "key": "register-run",
            "mode": "apply",
            "resource_key": "records",
            "operation": "INSERT",
            "risk": "medium",
            "approval_mode": "ask",
        }
    ]
    blueprint["source_traces"].append(
        {
            "target": "/tasks/0/document_prerequisites",
            "path": "SKILL.md",
            "line": 2,
            "reason": "Document processing requires the initial record to be saved.",
        }
    )
    return blueprint


def test_source_backed_prerequisite_survives_normalization_and_resolves_original_intent():
    """宣言は正規化で消えず、元 intent の operation/write slot に対応する。"""
    result = CapabilityBlueprintValidator(CONTRACTS).validate(_blueprint())
    assert result.blueprint["tasks"][0]["document_prerequisites"] == ["register-run"]
    manifest = {
        "capability_blueprint": result.blueprint,
        "tools": [{"capability": "document.readiness/v1", "required": True}],
    }
    assert [
        (p.intent_key, p.resource_key, p.operation)
        for p in document_prerequisites(manifest, "advise")
    ] == [("register-run", "records", "INSERT")]


@pytest.mark.parametrize(
    "case", ["trace", "intent", "observe", "resource", "empty", "duplicate", "null"]
)
def test_invalid_or_untraced_gate_is_not_an_executable_requirement(case):
    """推測された手順や壊れた参照を必須条件へ格上げしない。"""
    blueprint = _blueprint()
    if case == "trace":
        blueprint["source_traces"].pop()
    elif case == "intent":
        blueprint["effect_intents"] = []
    elif case == "observe":
        blueprint["effect_intents"][0] = {
            "key": "register-run",
            "mode": "observe",
            "operation": "INSERT",
            "risk": "low",
        }
    elif case == "resource":
        blueprint["resource_requirements"][0]["access"] = "read"
    else:
        blueprint["tasks"][0]["document_prerequisites"] = {
            "empty": [],
            "duplicate": ["register-run"] * 2,
            "null": None,
        }[case]
    with pytest.raises(CapabilityBlueprintError):
        CapabilityBlueprintValidator(CONTRACTS).validate(blueprint)


@pytest.mark.parametrize("required", [None, False])
def test_gate_cannot_be_published_without_required_runtime_capability(required):
    """新 field を知らない旧 Worker が読取へ進むことを required Tool 解決失敗で防ぐ。"""
    manifest = {"capability_blueprint": _blueprint(), "tools": []}
    if required is not None:
        manifest["tools"] = [{"capability": "document.readiness/v1", "required": required}]
    with pytest.raises(ValueError, match="readiness capability"):
        document_prerequisites(manifest, "advise")


def test_legacy_task_is_not_rewritten_with_an_invented_gate():
    """原文にない条件を追加せず、既存 Task の省略を保持する。"""
    blueprint = _minimal_blueprint()
    original = deepcopy(blueprint)
    assert document_prerequisites({"capability_blueprint": blueprint}, "advise") == ()
    assert blueprint == original
