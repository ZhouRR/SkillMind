"""Result 引用核対の五項目、旧履歴、形式との対応を公開契約全体で検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator

from projectmind.api.main import create_app
from projectmind.api.result_validation import result_validation_response

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _load(path: str) -> dict[str, Any]:
    """外部 URL を解決せず、リポジトリに保存した合成 JSON だけを使う。"""

    value = json.loads((CONTRACTS / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.fixture
def validators() -> tuple[Draft202012Validator, ...]:
    """手書き Schema と実 app OpenAPI に同じ応答・拒否条件を適用する。"""

    openapi = create_app().openapi()
    return (
        Draft202012Validator(_load("runs/detail/v1.schema.json")),
        Draft202012Validator({
            "$ref": "#/components/schemas/RunDetailResponse",
            "components": openapi["components"],
        }),
    )


@pytest.mark.parametrize("example", ["run-detail.v1.json", "run-detail-documents.v1.json"])
def test_legacy_and_checked_examples_match_contract_and_openapi(
    validators: tuple[Draft202012Validator, ...], example: str,
) -> None:
    """旧 example は保証を欠いたまま、新 example は明示範囲のまま受理する。"""

    payload = _load(f"examples/{example}")
    for validator in validators:
        validator.validate(payload)
    assert ("reference_checks" in payload["result"]["validation"]) == (
        example == "run-detail-documents.v1.json"
    )


def test_structured_output_requires_not_applicable_effect_checks(
    validators: tuple[Draft202012Validator, ...],
) -> None:
    """旧形式の新検証も受理するが、包絡の効果核対を名乗らせない。"""

    payload = _load("examples/run-detail-documents.v1.json")
    payload["result"]["result_kind"] = "STRUCTURED_OUTPUT"
    for validator in validators:
        assert not validator.is_valid(payload)
    payload["result"]["validation"]["reference_checks"]["effects"] = "NOT_APPLICABLE"
    for validator in validators:
        validator.validate(payload)


@pytest.mark.parametrize("field", ["version", "evidence", "proposals", "effects", "artifacts"])
def test_present_reference_checks_never_defaults_a_missing_field(
    validators: tuple[Draft202012Validator, ...], field: str,
) -> None:
    """可選なのは全体の有無だけであり、一部不足を通過させない。"""

    payload = _load("examples/run-detail-documents.v1.json")
    del payload["result"]["validation"]["reference_checks"][field]
    for validator in validators:
        assert not validator.is_valid(payload)


@pytest.mark.parametrize("field,value", [
    ("version", "projectmind.result-reference-checks/v2"),
    ("evidence", "VERIFIED"), ("proposals", False),
    ("effects", "NOT_APPLICABLE"), ("artifacts", "VERIFIED"),
    ("internal", "synthetic-private"),
])
def test_reference_checks_are_closed_and_scope_is_not_overstated(
    validators: tuple[Draft202012Validator, ...], field: str, value: object,
) -> None:
    """未知版・未定義の保証・追加 field は OpenAPI でも拒否する。"""

    payload = _load("examples/run-detail-documents.v1.json")
    payload["result"]["validation"]["reference_checks"][field] = value
    for validator in validators:
        assert not validator.is_valid(payload)


@pytest.mark.parametrize("value", [None, [], {}, True, "verified"])
def test_null_or_malformed_checks_are_not_legacy_absence(
    validators: tuple[Draft202012Validator, ...], value: object,
) -> None:
    """旧結果との互換を理由に壊れた新 field を省略扱いにしない。"""

    payload = _load("examples/run-detail-documents.v1.json")
    payload["result"]["validation"]["reference_checks"] = deepcopy(value)
    for validator in validators:
        assert not validator.is_valid(payload)


@pytest.mark.parametrize("kind,flag", [
    (kind, flag)
    for kind in ("OUTCOME_ENVELOPE", "STRUCTURED_OUTPUT")
    for flag in ("schema_valid", "evidence_refs_valid", "change_proposal_refs_valid")
] + [("OUTCOME_ENVELOPE", "outcome_envelope_valid")])
@pytest.mark.parametrize("missing", [False, True])
def test_checked_result_requires_consistent_success_flags_but_legacy_does_not(
    validators: tuple[Draft202012Validator, ...], kind: str, flag: str, missing: bool,
) -> None:
    """新版の矛盾だけを閉じ、原 flag が false/欠落の旧履歴には新 required を課さない。"""

    payload = _load("examples/run-detail-documents.v1.json")
    payload["result"]["result_kind"] = kind
    validation = payload["result"]["validation"]
    if kind == "STRUCTURED_OUTPUT":
        validation["reference_checks"]["effects"] = "NOT_APPLICABLE"
        del validation["outcome_envelope_valid"]
    if missing:
        del validation[flag]
    else:
        validation[flag] = False
    for validator in validators:
        assert not validator.is_valid(payload)
    del validation["reference_checks"]
    for validator in validators:
        validator.validate(payload)


def test_validation_public_allowlist_and_unavailable_problem_are_declared(
    validators: tuple[Draft202012Validator, ...],
) -> None:
    """内部 metadata を公開 contract に含めず、破損時の静的 503 を宣言する。"""

    payload = _load("examples/run-detail.v1.json")
    payload["result"]["validation"]["internal"] = "synthetic-private"
    for validator in validators:
        assert not validator.is_valid(payload)
    operation = create_app().openapi()["paths"][
        "/api/v1/projects/{project_id}/runs/{run_id}/detail"
    ]["get"]
    response = operation["responses"]["503"]
    assert "run_result_validation_unavailable" in response["description"]
    assert set(response["content"]) == {"application/problem+json"}
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"


class EmptyEvidenceLookup:
    """参照の無い合成結果で実 validator の出力契約だけを確認する。"""

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """外部 I/O は行わず、意図せず Evidence を必要とする fixture は失敗させる。"""

        assert run_id.int != 0 and not refs
        return frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["OUTCOME_ENVELOPE", "STRUCTURED_OUTPUT"])
async def test_actual_validator_metadata_survives_projection_and_public_contract(
    validators: tuple[Draft202012Validator, ...], kind: str,
) -> None:
    """生産 validator→公開 projection→Schema の結線で field 漏れや意味の変化を検出する。"""

    from projectmind.agent.result_validation import ResultValidator

    payload = _load("examples/run-detail.v1.json")
    data = deepcopy(payload["result"]["data"]) if kind == "OUTCOME_ENVELOPE" else {
        "summary": "Synthetic structured result",
    }
    data["evidence_refs"] = []
    validated = await ResultValidator(EmptyEvidenceLookup()).validate(
        run_id=uuid4(), schema={"type": "object"}, schema_ref="schema://synthetic-result",
        structured_output=data, result_kind=kind,
    )
    projected = result_validation_response(validated.validation, result_kind=kind)
    assert projected == validated.validation
    assert projected["reference_checks"]["artifacts"] == "RUN_OWNERSHIP_AND_CONTENT"
    assert projected["reference_checks"]["version"] == "projectmind.result-reference-checks/v2"
    assert projected["artifact_refs_valid"] is True
    payload["result"].update({
        "result_kind": kind, "data": data, "evidence_refs": [], "validation": projected,
    })
    for validator in validators:
        validator.validate(payload)
