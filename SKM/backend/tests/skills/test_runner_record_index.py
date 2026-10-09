"""実行/判定 Skill が同じ共通理由索引を使い、全ステップを保持することを検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.core.hashing import canonical_json

SKILLS = Path(__file__).resolve().parents[4] / "real-flow-test/skills"


def schema(skill):
    """実際に取り込む自己完結 Schema を読む。平台へ業務形式を登録しない。"""
    return json.loads((SKILLS / skill / "schemas/runner-records.schema.json").read_text())


def index():
    """多数の同じ制約を持つ、合成の未実行ステップ索引を作る。"""
    return {
        "schemaVersion": "2.0", "executionId": str(uuid4()), "operations": [],
        "reasons": {"r1": {"text": "Required internal observation is unavailable. " * 8,
                            "evidenceRefs": ["ev_catalog"]}},
        "notRun": [{"testCaseId": f"CASE-{n:04}", "stepId": "S1", "reasonRef": "r1"}
                   for n in range(98)],
        "gaps": [{"testCaseId": "CASE-0100", "stepId": "S2", "reasonRef": "r1",
                  "detail": "This step also lacks its original request ID."}],
    }


def test_execution_and_judgement_accept_the_same_compact_index():
    """共通説明と根拠を一度だけ保存し、対象 98 項目と個別差分は縮めない。"""
    value = index()
    for skill in ["execution-plan-generator", "result-judge-triage"]:
        Draft202012Validator(schema(skill), format_checker=FormatChecker()).validate(value)
    assert schema("execution-plan-generator") == schema("result-judge-triage")
    assert len(value["notRun"]) == 98
    assert len({(s["testCaseId"], s["stepId"]) for s in value["notRun"]}) == 98
    assert all(s["reasonRef"] in value["reasons"] for s in [*value["notRun"], *value["gaps"]])
    repeated = deepcopy(value)
    for entry in repeated["notRun"]:
        entry["reason"] = value["reasons"]["r1"]["text"]
        entry["evidenceRefs"] = ["ev_catalog"]
    assert len(canonical_json(value).encode()) < len(canonical_json(repeated).encode()) / 3


@pytest.mark.parametrize("damage", ["version", "repeated_reason", "no_reference", "invalid_key"])
def test_new_index_rejects_ambiguous_or_repeated_note_shapes(damage):
    """新形式だけを生成し、原因参照なしや旧長文併記を契約で拒否する。"""
    value = index()
    if damage == "version":
        value["schemaVersion"] = "1.0"
    elif damage == "repeated_reason":
        value["notRun"][0]["reason"] = "repeated"
    elif damage == "no_reference":
        del value["notRun"][0]["reasonRef"]
    else:
        value["reasons"]["invalid/key"] = {"text": "invalid"}
    assert not Draft202012Validator(schema("execution-plan-generator")).is_valid(value)


def test_no_gaps_or_missing_steps_need_no_fabricated_reason():
    """不足がない実行にダミー原因や新しい確認工程を必須にしない。"""
    value = index()
    value.update(reasons={}, notRun=[], gaps=[])
    Draft202012Validator(schema("execution-plan-generator")).validate(value)
