"""保存済み Result の検証範囲を白名单で公開し、旧履歴へ保証を補わない。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, with_config
from typing_extensions import TypedDict

from projectmind.api.problems import ProblemException


@with_config(ConfigDict(extra="forbid", strict=True))
class ResultReferenceChecksV1Response(TypedDict):
    """参照核対の実施範囲。Artifact の存在や内容は検証済みと表示しない。"""

    version: Literal["projectmind.result-reference-checks/v1"]
    evidence: Literal["RUN_OWNERSHIP"]
    proposals: Literal["RUN_OWNERSHIP_AND_STATE"]
    effects: Literal["PLATFORM_RECORD_MATCH", "NOT_APPLICABLE"]
    artifacts: Literal["NOT_VERIFIED"]


@with_config(ConfigDict(extra="forbid", strict=True))
class ResultReferenceChecksV2Response(TypedDict):
    """保存時に同 Run の不可変 Artifact 原字節まで照合した検証範囲。"""

    version: Literal["projectmind.result-reference-checks/v2"]
    evidence: Literal["RUN_OWNERSHIP"]
    proposals: Literal["RUN_OWNERSHIP_AND_STATE"]
    effects: Literal["PLATFORM_RECORD_MATCH", "NOT_APPLICABLE"]
    artifacts: Literal["RUN_OWNERSHIP_AND_CONTENT"]


ResultReferenceChecksResponse = Annotated[
    ResultReferenceChecksV1Response | ResultReferenceChecksV2Response,
    Field(discriminator="version"),
]


@with_config(ConfigDict(extra="forbid", strict=True))
class RunResultValidationResponse(TypedDict, total=False):
    """既存 field の省略/null を保ち、公開してよい検証 metadata だけを定義する。"""

    schema_ref: str
    schema_valid: bool
    outcome_envelope_valid: bool
    outcome_version: str
    task_schema_valid: bool | None
    task_schema_ref: str | None
    evidence_refs_valid: bool
    artifact_refs_valid: bool
    evidence_count: Annotated[int, Field(ge=0)]
    artifact_count: Annotated[int, Field(ge=0)]
    change_proposal_count: Annotated[int, Field(ge=0)]
    change_proposal_refs_valid: bool
    reference_checks: ResultReferenceChecksResponse


_VALIDATION_ADAPTER = TypeAdapter(RunResultValidationResponse)


def _reference_check_flags(result_kind: str) -> tuple[str, ...]:
    """新核対の宣言と同時に成立すべき既存 flag を結果形式ごとに固定する。"""

    common = ("schema_valid", "evidence_refs_valid", "change_proposal_refs_valid")
    return (*common, "outcome_envelope_valid") if result_kind == "OUTCOME_ENVELOPE" else common


def result_validation_response(
    validation: object, *, result_kind: str
) -> RunResultValidationResponse:
    """内部 metadata を除き、壊れた新保証は原値を変えず静的な読取失敗へ閉じる。"""

    try:
        if not isinstance(validation, dict):
            raise ValueError("Invalid stored validation")
        projected = _VALIDATION_ADAPTER.validate_python(
            {
                key: value
                for key, value in validation.items()
                if key in RunResultValidationResponse.__annotations__
            }
        )
        checks = projected.get("reference_checks")
        if checks is not None:
            expected = {
                "OUTCOME_ENVELOPE": "PLATFORM_RECORD_MATCH",
                "STRUCTURED_OUTPUT": "NOT_APPLICABLE",
            }.get(result_kind)
            if checks["effects"] != expected:
                raise ValueError("Stored reference checks do not match the result kind")
            if any(projected.get(flag) is not True for flag in _reference_check_flags(result_kind)):
                raise ValueError("Stored reference checks contradict their validation flags")
            if (
                checks["version"] == "projectmind.result-reference-checks/v2"
                and projected.get("artifact_refs_valid") is not True
            ):
                raise ValueError("Stored Artifact checks contradict their validation flag")
            if (
                checks["version"] == "projectmind.result-reference-checks/v1"
                and projected.get("artifact_refs_valid") is True
            ):
                raise ValueError("Unverified Artifact scope cannot claim verified references")
        return projected
    except (ValidationError, ValueError) as error:
        raise ProblemException(
            status=503,
            title="Result validation is unavailable",
            detail="The saved result validation metadata could not be verified.",
            code="run_result_validation_unavailable",
            headers={"Cache-Control": "no-store"},
        ) from error


def result_reference_checks_schema(schema: dict[str, Any]) -> None:
    """JSON Schema でも包絡/旧構造化出力と新核対範囲の対応を固定する。"""

    schema["allOf"] = [
        {
            "if": {"properties": {"result_kind": {"const": kind}}},
            "then": {
                "properties": {
                    "validation": {
                        "properties": {
                            "reference_checks": {
                                "properties": {"effects": {"const": effects}}
                            }
                        },
                        "allOf": [{
                            "if": {"required": ["reference_checks"]},
                            "then": {
                                "required": list(_reference_check_flags(kind)),
                                "properties": {
                                    flag: {"const": True}
                                    for flag in _reference_check_flags(kind)
                                },
                            },
                        }, {
                            "if": {
                                "required": ["reference_checks"],
                                "properties": {"reference_checks": {
                                    "required": ["version"],
                                    "properties": {"version": {
                                        "const": "projectmind.result-reference-checks/v2",
                                    }},
                                }},
                            },
                            "then": {
                                "required": ["artifact_refs_valid"],
                                "properties": {"artifact_refs_valid": {"const": True}},
                            },
                        }, {
                            "if": {
                                "required": ["reference_checks"],
                                "properties": {"reference_checks": {
                                    "required": ["version"],
                                    "properties": {"version": {
                                        "const": "projectmind.result-reference-checks/v1",
                                    }},
                                }},
                            },
                            "then": {
                                "properties": {"artifact_refs_valid": {"const": False}},
                            },
                        }],
                    }
                }
            },
        }
        for kind, effects in (
            ("OUTCOME_ENVELOPE", "PLATFORM_RECORD_MATCH"),
            ("STRUCTURED_OUTPUT", "NOT_APPLICABLE"),
        )
    ]
