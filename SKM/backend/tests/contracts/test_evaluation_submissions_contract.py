"""追加式旧契約を保持したまま、原要求と履歴ページの独立契約を照合する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.api.main import create_app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
PREFIX = "/api/v1/projects/{project_id}/runs/{run_id}"
NIL = "00000000-0000-0000-0000-000000000000"


def _load(relative: str) -> dict[str, Any]:
    """外部参照を解決せず、合成された repository JSON object のみ読む。"""

    value = json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    """実 application の宣言を一度生成し、関連する独立契約と比較する。"""

    return create_app().openapi()


def _validators(
    openapi: dict[str, Any], schema: str, component: str,
) -> tuple[Draft202012Validator, ...]:
    """同じ payload に手書き契約と OpenAPI を適用する。"""

    return (
        Draft202012Validator(_load(f"evaluations/v1/{schema}.schema.json"),
                              format_checker=FormatChecker()),
        Draft202012Validator({"$ref": f"#/components/schemas/{component}",
                              "components": openapi["components"]},
                             format_checker=FormatChecker()),
    )


@pytest.mark.parametrize("schema,component,example", [
    ("submission-request", "SubmitEvaluationRequest", "evaluation-submission-request.v1.json"),
    ("submission", "EvaluationSubmissionResponse", "evaluation-submission.v1.json"),
    ("page", "EvaluationPageResponse", "evaluation-page.v1.json"),
])
def test_new_examples_match_exact_top_level_contracts(
    openapi: dict[str, Any], schema: str, component: str, example: str,
) -> None:
    """原要求・回执・ページを登録し、追加の内部 field は受理しない。"""

    payload = _load(f"examples/{example}")
    for validator in _validators(openapi, schema, component):
        validator.validate(payload)
        candidate = deepcopy(payload)
        candidate["request_hash"] = "sha256:" + "0" * 64
        assert not validator.is_valid(candidate)
        for field in payload:
            if field in {"comment", "revisions"} and schema == "submission-request":
                continue
            candidate = deepcopy(payload)
            del candidate[field]
            assert not validator.is_valid(candidate)


@pytest.mark.parametrize("field,value", [
    ("submission_key", NIL), ("result_id", NIL),
    ("submission_key", "invalid"), ("result_id", "invalid"),
    ("rating", True), ("rating", "4"), ("rating", 0), ("rating", 6),
    ("comment", None), ("revisions", None), ("verdict", "approved"),
    ("user_id", "00000000-0000-4000-8000-000000000111"),
])
def test_new_submission_rejects_invalid_identity_and_values(
    openapi: dict[str, Any], field: str, value: object,
) -> None:
    """人工評価を actor 偽装や効果の承認と混同しない。"""

    payload = _load("examples/evaluation-submission-request.v1.json")
    payload[field] = value
    assert all(not item.is_valid(payload) for item in _validators(
        openapi, "submission-request", "SubmitEvaluationRequest",
    ))


def test_request_defaults_remain_optional_without_widening_old_request(
    openapi: dict[str, Any],
) -> None:
    """省略と明示 default は合法だが、新しい key を旧 POST の契約へ混ぜない。"""

    payload = _load("examples/evaluation-submission-request.v1.json")
    del payload["comment"], payload["revisions"]
    for validator in _validators(openapi, "submission-request", "SubmitEvaluationRequest"):
        validator.validate(payload)
    old = Draft202012Validator(_load("evaluations/v1/create-request.schema.json"))
    assert not old.is_valid(payload)
    del payload["submission_key"], payload["result_id"]
    old.validate(payload)


@pytest.mark.parametrize("field,value", [
    ("evaluation_id", NIL), ("user_id", NIL), ("run_id", NIL), ("result_id", NIL),
    ("created_at", "2026-09-10T12:00:00"), ("rating", True), ("rating", 0),
    ("comment", None), ("revisions", [{}]), ("internal_session_id", NIL),
])
def test_receipt_keeps_original_nine_fields_with_strict_saved_shape(
    openapi: dict[str, Any], field: str, value: object,
) -> None:
    """内部第十 field、壊れた日時、原値の欠落を原要求の成功として公開しない。"""

    payload = _load("examples/evaluation-submission.v1.json")
    assert len(payload["evaluation"]) == 9
    payload["evaluation"][field] = value
    assert all(not item.is_valid(payload) for item in _validators(
        openapi, "submission", "EvaluationSubmissionResponse",
    ))


def test_null_original_value_is_not_missing_or_replaced(openapi: dict[str, Any]) -> None:
    """元の null は合法 JSON 値として保持し、欠落 field と区別する。"""

    payload = _load("examples/evaluation-submission.v1.json")
    revision = payload["evaluation"]["revisions"][0]
    revision["original_value"] = None
    revision["suggested_value"] = None
    for validator in _validators(openapi, "submission", "EvaluationSubmissionResponse"):
        validator.validate(payload)
    del revision["original_value"]
    assert all(not item.is_valid(payload) for item in _validators(
        openapi, "submission", "EvaluationSubmissionResponse",
    ))


def test_page_is_bounded_with_explicit_nullable_cursor(openapi: dict[str, Any]) -> None:
    """ページは総数や snapshot を含めず、空履歴も Result identity を持つ。"""

    payload = _load("examples/evaluation-page.v1.json")
    for validator in _validators(openapi, "page", "EvaluationPageResponse"):
        validator.validate({**payload, "items": [], "next_cursor": None})
        assert not validator.is_valid({**payload, "items": payload["items"] * 101})
        assert not validator.is_valid({**payload, "next_cursor": NIL})
        assert not validator.is_valid({**payload, "total": 1})


def test_openapi_declares_actual_status_problem_media_and_no_store(
    openapi: dict[str, Any],
) -> None:
    """新旧操作の成功 status と静的失敗を明示し、未実装の nosniff は宣言しない。"""

    paths = openapi["paths"]
    entries = (
        ("/evaluations", "post", {"201", "400", "401", "403", "404", "409", "422", "503"}),
        ("/evaluations", "get", {"200", "401", "404", "409", "422", "503"}),
        ("/evaluation-submissions", "post",
         {"200", "201", "400", "401", "403", "404", "409", "422", "503"}),
        ("/evaluation-submissions/{submission_key}", "get",
         {"200", "401", "404", "409", "422", "503"}),
        ("/evaluations/page", "get", {"200", "400", "401", "404", "409", "422", "503"}),
    )
    for suffix, method, expected in entries:
        operation = paths[PREFIX + suffix][method]
        assert set(operation["responses"]) == expected
        assert "auth" in operation["tags"]
        for code, response in operation["responses"].items():
            assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
            assert "X-Request-ID" in response["headers"]
            assert "X-Content-Type-Options" not in response["headers"]
            assert set(response["content"]) == {
                "application/problem+json" if int(code) >= 400 else "application/json"
            }
    operation = paths[PREFIX + "/evaluation-submissions/{submission_key}"]["get"]
    result = next(item for item in operation["parameters"] if item["name"] == "result_id")
    assert result["in"] == "query" and result["required"] is True
    assert result["schema"]["not"]["const"] == NIL
    page = paths[PREFIX + "/evaluations/page"]["get"]
    limit = next(item for item in page["parameters"] if item["name"] == "limit")
    assert limit["schema"]["minimum"] == 1 and limit["schema"]["maximum"] == 100
    assert limit["schema"]["default"] == 20
