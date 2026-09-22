"""原記録を直接判定する Skill パッケージと業務引渡し契約を検証する。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    """共有 Schema の正本を取得する。"""
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def validator(name: str) -> Draft202012Validator:
    """実 format と同じ root 内の参照を使い、外部取得を行わない。"""
    schema = load(name)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture
def basis() -> dict:
    """原文位置で固定した二つの依存ステップ。元の期待条件そのものは複写しない。"""
    return {
        "sourceMarkdown": {"documentLibraryId": "fixture", "bucket": "fixture",
                           "objectKey": "仕様.md", "sha256": "sha256:" + "a" * 64},
        "specVersion": "sha256:" + "b" * 64,
        "environment": {"appId": "fixture", "connectionRef": "fixture-runner",
                        "environmentLease": "fixture-lease", "maxParallelism": 1,
                        "caseTimeoutSeconds": 1800},
        "cases": [{"testCaseId": "TC-1", "steps": [
            {"stepId": "S1", "sourceLines": {"start": 3, "end": 3},
             "expectedLines": {"start": 4, "end": 4}, "dependsOn": []},
            {"stepId": "S2", "sourceLines": {"start": 5, "end": 5},
             "expectedLines": {"start": 6, "end": 6},
             "dependsOn": [{"testCaseId": "TC-1", "stepId": "S1"}]},
        ]}],
    }


@pytest.fixture
def records() -> dict:
    """合成の原 locator だけを持つ DB result_ref。Runner 本文の形式ではない。"""
    return {
        "schemaVersion": "1.0", "executionId": "00000000-0000-4000-8000-000000000003",
        "operations": [{
            "testCaseId": "TC-1", "stepId": "S1", "role": "BUSINESS",
            "connectionRef": "fixture-runner", "requestId": "fixture-request-1",
            "confirmation": "CONFIRMED", "records": [{"locator": "fixture-record-1"}],
        }],
        "notRun": [{"testCaseId": "TC-1", "stepId": "S2", "reason": "依存操作が失敗したため未送信"}],
        "gaps": [],
    }


@pytest.fixture
def verdict(basis: dict, records: dict) -> dict:
    """実行/版は DB、実測は合成の原記録に由来する一ケースの判定。"""
    return {
        "schemaVersion": "3.0", "testRunId": "00000000-0000-4000-8000-000000000001",
        "documentId": "00000000-0000-4000-8000-000000000002",
        "executionId": records["executionId"], "testCaseId": "TC-1",
        "specVersion": basis["specVersion"], "sourceMarkdown": basis["sourceMarkdown"],
        "versionSnapshot": {"skillVersion": "fixture", "environmentVersion": "fixture",
                            "sutBuild": "fixture", "runnerVersions": {"runner": "fixture"},
                            "modelVersion": "fixture-executor"},
        "judgementModelVersion": "fixture-judge", "executionStatus": "ERROR",
        "verdict": "INCONCLUSIVE", "confidence": 0.2, "classification": "UNKNOWN",
        "assessments": [
            {"stepId": "S1", "expectedResult": "仕様に定めた値", "actualResult": None,
             "executionStatus": None, "satisfied": None, "evidenceRefs": []},
            {"stepId": "S2", "expectedResult": "仕様に定めた結果", "actualResult": None,
             "executionStatus": "NOT_RUN", "satisfied": None, "evidenceRefs": []},
        ],
        "missingEvidence": ["S1 原記録の内容が取得できない"], "counterEvidence": [],
        "failureFingerprint": "sha256:" + "c" * 64,
        "fingerprintBasis": {"case": "TC-1", "reason": "missing_record"},
        "knownIssueCheck": "UNAVAILABLE", "knownIssues": [], "recommendedActions": ["原要求を照会する"],
    }


def test_native_handoff_needs_neither_plan_nor_generated_result(basis: dict, records: dict) -> None:
    """原文根拠と参照索引だけを検証し、原記録を変換する契約を要求しない。"""
    validator("execution-basis.schema.json").validate(basis)
    validator("runner-records.schema.json").validate(records)
    assert "executionBasis" not in records and "actualResult" not in records
    assert "sourcePlan" not in records


@pytest.mark.parametrize("change", ["no_cases", "no_steps", "no_expected", "bad_hash", "no_environment"])
def test_invalid_basis_is_rejected(basis: dict, change: str) -> None:
    """全対象/元の期待条件/環境を省略して高速化しない。"""
    if change == "no_cases":
        basis["cases"] = []
    elif change == "no_steps":
        basis["cases"][0]["steps"] = []
    elif change == "no_expected":
        basis["cases"][0]["steps"][0].pop("expectedLines")
    elif change == "bad_hash":
        basis["sourceMarkdown"]["sha256"] = "unknown"
    else:
        basis.pop("environment")
    assert list(validator("execution-basis.schema.json").iter_errors(basis))


@pytest.mark.parametrize("change", ["missing_id", "bad_uuid", "missing_connection", "missing_request",
                                   "bad_confirmation", "empty_locator", "embedded_body", "copied_basis",
                                   "duplicate_operation", "false_pass", "missing_not_run", "missing_gaps"])
def test_invalid_handoff_is_rejected(records: dict, change: str) -> None:
    """参照索引の欠落と原文/成功の再生成を拒否する。所有の実確認とは別検証。"""
    op = records["operations"][0]
    if change == "missing_id":
        records.pop("executionId")
    elif change == "bad_uuid":
        records["executionId"] = "not-an-id"
    elif change == "missing_connection":
        op.pop("connectionRef")
    elif change == "missing_request":
        op.pop("requestId")
    elif change == "bad_confirmation":
        op["confirmation"] = "SUCCESS"
    elif change == "empty_locator":
        op["records"][0]["locator"] = ""
    elif change == "embedded_body":
        op["records"][0]["body"] = "copied report"
    elif change == "copied_basis":
        records["executionBasis"] = {"cases": []}
    elif change == "duplicate_operation":
        records["operations"].append(deepcopy(op))
    elif change == "false_pass":
        op["verdict"] = "PASS"
    elif change == "missing_not_run":
        records.pop("notRun")
    else:
        records.pop("gaps")
    assert list(validator("runner-records.schema.json").iter_errors(records))


def test_confirmed_failure_and_unconfirmed_are_not_business_verdicts(records: dict) -> None:
    """参照索引は終端確認だけを持ち、実測・失敗状態は原記録から判定する。"""
    records["operations"][0]["confirmation"] = "UNCONFIRMED"
    records["operations"][0]["records"] = []
    records["gaps"] = [{"testCaseId": "TC-1", "stepId": "S1", "reason": "原応答未確認"}]
    validator("runner-records.schema.json").validate(records)
    assert records["notRun"][0]["stepId"] == "S2"


def test_empty_operation_set_can_record_preparation_failure(records: dict) -> None:
    """操作を発行できなかった異常のために架空の Runner 記録を要求しない。"""
    records["operations"] = []
    records["notRun"].append({"testCaseId": "TC-1", "stepId": "S1", "reason": "準備失敗で未送信"})
    validator("runner-records.schema.json").validate(records)


def test_multiple_operations_can_map_to_one_business_step(records: dict) -> None:
    """起動や観測を別の業務ステップとして水増ししない。"""
    for role, request in [("PREPARATION", "fixture-start"), ("OBSERVATION", "fixture-observe")]:
        op = deepcopy(records["operations"][0])
        op.update(role=role, requestId=request, records=[{"locator": request}])
        records["operations"].append(op)
    validator("runner-records.schema.json").validate(records)
    assert len({op["stepId"] for op in records["operations"]}) == 1


def test_native_reference_needs_no_forced_document_library_copy(verdict: dict) -> None:
    """原 locator と接続/要求で原記録を引用し、全ファイルの二重保存を要求しない。"""
    evidence = {"connectionRef": "fixture-runner", "requestId": "fixture-request-1",
                "record": {"locator": "fixture-record-1"}}
    verdict["assessments"][0]["evidenceRefs"] = [evidence]
    verdict["assessments"][0]["actualResult"] = "hallo\r"
    validator("verdict.schema.json").validate(verdict)
    assert verdict["assessments"][0]["actualResult"].endswith("\r")


@pytest.mark.parametrize("change", ["no_connection", "no_request", "bare_path", "old_library_ref", "bad_hash"])
def test_incomplete_native_evidence_identity_is_rejected(verdict: dict, change: str) -> None:
    """原接続・要求の無い単なるパスを判定 Evidence としない。"""
    evidence = {"connectionRef": "fixture-runner", "requestId": "fixture-request-1",
                "record": {"locator": "fixture-record-1"}}
    if change == "no_connection":
        evidence.pop("connectionRef")
    elif change == "no_request":
        evidence.pop("requestId")
    elif change == "bare_path":
        evidence = "C:\\fixture\\report.json"
    elif change == "old_library_ref":
        evidence = {"documentLibraryId": "fixture", "bucket": "fixture", "objectKey": "report.json"}
    else:
        evidence["record"]["sha256"] = "made-up"
    verdict["assessments"][0]["evidenceRefs"] = [evidence]
    assert list(validator("verdict.schema.json").iter_errors(verdict))


@pytest.mark.parametrize("change", [None, "missing_hash", "url_as_object_key", "missing_original"])
def test_archive_keeps_native_identity(records: dict, change: str | None) -> None:
    """退避は元参照に追記し、取得 hash や元 locator を省略しない。"""
    record = records["operations"][0]["records"][0]
    record["archive"] = {"documentLibraryId": "fixture", "bucket": "fixture",
                         "objectKey": "records/original.json", "sha256": "sha256:" + "e" * 64}
    if change == "missing_hash":
        record["archive"].pop("sha256")
    elif change == "url_as_object_key":
        record["archive"]["objectKey"] = "https://example.invalid/report"
    elif change == "missing_original":
        record.pop("locator")
    errors = list(validator("runner-records.schema.json").iter_errors(records))
    assert bool(errors) is (change is not None)


def make_pass(verdict: dict) -> dict:
    """独立した期待値を満たす合成判定。原記録の存在/内容の実証ではない。"""
    verdict.update(verdict="PASS", executionStatus="COMPLETED", classification=None,
                   missingEvidence=[], failureFingerprint=None, fingerprintBasis=None)
    for position, assessment in enumerate(verdict["assessments"], 1):
        assessment.update(executionStatus="COMPLETED", satisfied=True, actualResult="観測済みの値",
                          evidenceRefs=[{"connectionRef": "fixture-runner", "requestId": f"fixture-{position}",
                                         "record": {"locator": f"fixture-record-{position}"}}])
    return verdict


def test_pass_and_product_payload_reference_native_records(verdict: dict) -> None:
    """判定と不具合のどちらも原記録参照を使用できる。"""
    validator("verdict.schema.json").validate(make_pass(verdict))
    defect = {key: deepcopy(verdict[key]) for key in (
        "schemaVersion", "executionId", "testRunId", "documentId", "testCaseId", "versionSnapshot", "sourceMarkdown"
    )}
    defect.update(classification="PRODUCT", failureFingerprint="sha256:" + "d" * 64,
                  disposition="NEW_CANDIDATE", title="合成の不一致", expectedResult="期待値",
                  actualResult="異なる値", reproductionSteps=["原仕様の操作"],
                  evidenceRefs=verdict["assessments"][0]["evidenceRefs"], knownIssues=[])
    validator("defect.schema.json").validate(defect)


@pytest.mark.parametrize("change", ["unknown", "timeout", "not_run", "no_evidence", "missing_evidence",
                                   "counter_evidence", "unsatisfied", "no_actual", "no_assessments"])
def test_pass_cannot_hide_missing_or_contradictory_results(verdict: dict, change: str) -> None:
    """構造上の不足・不明を PASS にしない。原文の意味と所有は別途照合が必要。"""
    value = make_pass(verdict)
    step = value["assessments"][0]
    if change == "unknown":
        step["executionStatus"] = None
    elif change == "timeout":
        step["executionStatus"] = "TIMEOUT"
    elif change == "not_run":
        step["executionStatus"] = "NOT_RUN"
    elif change == "no_evidence":
        step["evidenceRefs"] = []
    elif change == "missing_evidence":
        value["missingEvidence"] = ["画像を確認できない"]
    elif change == "counter_evidence":
        value["counterEvidence"] = ["原記録と矛盾する値"]
    elif change == "unsatisfied":
        step["satisfied"] = False
    elif change == "no_actual":
        step["actualResult"] = None
    else:
        value["assessments"] = []
    assert list(validator("verdict.schema.json").iter_errors(value))


def test_packaged_schemas_and_links_are_self_contained() -> None:
    """実行/判定 Skill の単独導入で外部 Schema や隣接 package を要求しない。"""
    for skill in ("execution-plan-generator", "result-judge-triage"):
        package = ROOT / "skills" / skill
        text = (package / "SKILL.md").read_text(encoding="utf-8")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            target = (package / link).resolve()
            assert target.is_relative_to(package.resolve()) and target.is_file()
        for path in (package / "schemas").glob("*.json"):
            assert path.read_bytes() == (ROOT / "schemas" / path.name).read_bytes()
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            for ref in re.findall(r'"\$ref"\s*:\s*"([^"]+)"', path.read_text(encoding="utf-8")):
                assert ref.startswith("#/")
                node = schema
                for token in ref[2:].split("/"):
                    node = node[token.replace("~1", "/").replace("~0", "~")]


def test_obsolete_intermediate_contracts_are_removed() -> None:
    """古い中間報告/計画への入力依存を残さず、並行モードも作らない。"""
    assert not list(ROOT.rglob("execution-result.schema.json"))
    assert not list(ROOT.rglob("execution-plan.schema.json"))
    for skill in ("execution-plan-generator", "result-judge-triage"):
        text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        assert "execution-result.schema.json" not in text


def test_ref_contracts_have_one_record_definition() -> None:
    """引渡し索引と判定/不具合が同じ原記録参照を使用する。"""
    records = load("runner-records.schema.json")
    verdict = load("verdict.schema.json")
    assert records["$defs"]["record"] == verdict["$defs"]["runnerRecord"]
    assert verdict["$defs"] == load("defect.schema.json")["$defs"]
    for node in (verdict, load("defect.schema.json")):
        assert node["properties"]["schemaVersion"]["const"] == "3.0"
        assert "sourceMarkdown" in node["required"] and "sourcePlan" not in node["properties"]


@pytest.mark.parametrize("phrase", [
    "追加報告の生成・保存を理由に次の操作を待たせない",
    "会話の記憶だけに依存しない",
    "画像不足だけで実際に完了した操作を未実行へ変更しない",
    "UI 操作を再送しない",
    "終端状態、`finished_at` を一回の UPDATE で同時保存・回読する",
    "同じ行の観測と仕様上の対象",
])
def test_execution_prompt_preserves_business_boundaries(phrase: str) -> None:
    """指示の静的回帰であり、実モデルの解釈等価性の検証ではない。"""
    text = (ROOT / "skills/execution-plan-generator/SKILL.md").read_text(encoding="utf-8")
    assert phrase in text
    assert "保存失敗時は次の操作へ進まない" not in text


@pytest.mark.parametrize("phrase", [
    "取得できた報告だけへ範囲を縮めない",
    "原記録がないだけでは `NOT_RUN` とせず",
    "スクリーンショットは内容を実際に見られた場合だけ",
    "日時・ディレクトリ名・画面名の近さで混ぜない",
    "クリック完了から受信端到達を推論せず",
    "DB の `version_snapshot` から引き継ぎ",
    "UI 操作・起動・送信を再実行しない",
])
def test_judgement_prompt_uses_original_sources(phrase: str) -> None:
    """消えた中間ファイルから版や実測を取得する残存指示を防ぐ。"""
    text = (ROOT / "skills/result-judge-triage/SKILL.md").read_text(encoding="utf-8")
    assert phrase in text
    assert "versionSnapshot` は実行結果から" not in text
