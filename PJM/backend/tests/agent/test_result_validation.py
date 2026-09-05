"""ResultValidator の Schema、Evidence 所有、finding 最低要件を検証する。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from projectmind.agent.outcome import compile_outcome_schema
from projectmind.agent.result_validation import ResultValidationError, ResultValidator

SCHEMA_CHECKSUM = "sha256:" + ("c" * 64)


def _validator(refs: frozenset[str]) -> ResultValidator:
    """Business schema path に依存しない ResultValidator を組み立てる。"""

    return ResultValidator(MemoryEvidenceLookup(refs))


class MemoryEvidenceLookup:
    """Run ごとの Evidence reference を返す test lookup。"""

    def __init__(self, refs: frozenset[str]) -> None:
        """存在扱いにする reference を保持する。"""

        self.refs = refs

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """要求集合と test data の積集合を返す。"""

        assert isinstance(run_id, UUID)
        return refs & self.refs


def _schema() -> dict[str, object]:
    """Evidence を含む Generated output Schema を返す。"""

    return {
        "type": "object",
        "required": ["summary", "evidence_refs"],
        "properties": {
            "summary": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }


def _result() -> dict[str, object]:
    """Generated Schema に適合する汎用 result を返す。"""

    return {"summary": "Repository review complete", "evidence_refs": ["ev_issue_001"]}


@pytest.mark.asyncio
async def test_valid_result_requires_owned_evidence() -> None:
    """Schema を満たし、同じ Run の Evidence を参照する結果を受理する。"""

    validated = await _validator(frozenset({"ev_issue_001"})).validate(
        run_id=uuid4(),
        schema=_schema(),
        schema_ref=SCHEMA_CHECKSUM,
        structured_output=_result(),
    )

    assert validated.evidence_refs == frozenset({"ev_issue_001"})
    assert validated.summary == "Repository review complete"
    assert validated.confidence is None
    assert validated.needs_review is False
    assert validated.validation["schema_valid"] is True


@pytest.mark.asyncio
async def test_foreign_evidence_reference_is_rejected() -> None:
    """形式が正しくても現在 Run に属さない Evidence ID を拒否する。"""

    with pytest.raises(ResultValidationError) as captured:
        await _validator(frozenset()).validate(
            run_id=uuid4(),
            schema=_schema(),
            schema_ref=SCHEMA_CHECKSUM,
            structured_output=_result(),
        )

    assert captured.value.code == "evidence_reference_invalid"
    assert "ev_issue_001" not in captured.value.message


@pytest.mark.asyncio
async def test_schema_invalid_result_does_not_expose_field_value() -> None:
    """Schema error は path だけを返し、Agent の field 値を error に複製しない。"""

    result = _result()
    result["summary"] = {"secret": "secret-invalid-value"}

    with pytest.raises(ResultValidationError) as captured:
        await _validator(frozenset({"ev_issue_001"})).validate(
            run_id=uuid4(),
            schema=_schema(),
            schema_ref=SCHEMA_CHECKSUM,
            structured_output=result,
        )

    assert captured.value.code == "result_schema_invalid"
    assert "secret-invalid-value" not in captured.value.message


@pytest.mark.asyncio
async def test_generic_result_uses_convention_summary_confidence_and_evidence() -> None:
    """通用 schema は top-level summary/confidence/needs_review と Evidence 所有だけで受理する。"""

    validated = await ResultValidator(MemoryEvidenceLookup(frozenset({"ev_generic_1"}))).validate(
        run_id=uuid4(),
        schema={"type": "object", "additionalProperties": True, "required": ["summary"]},
        schema_ref=SCHEMA_CHECKSUM,
        structured_output={
            "summary": "Repository review complete",
            "confidence": 0.42,
            "needs_review": True,
            "source_ref": "ev_generic_1",
        },
    )

    assert validated.summary == "Repository review complete"
    assert validated.confidence == pytest.approx(0.42)
    assert validated.needs_review is True
    assert validated.evidence_refs == frozenset({"ev_generic_1"})


@pytest.mark.asyncio
async def test_generic_result_falls_back_to_neutral_defaults() -> None:
    """通用 convention field が無い結果は中立な既定値へ畳み、JAF 文案を出さない。"""

    validated = await ResultValidator(MemoryEvidenceLookup(frozenset())).validate(
        run_id=uuid4(),
        schema={"type": "object", "additionalProperties": True},
        schema_ref=SCHEMA_CHECKSUM,
        structured_output={"findings": []},
    )

    assert validated.summary == "Structured result recorded"
    assert "JAF" not in validated.summary
    assert validated.confidence is None
    assert validated.needs_review is False


@pytest.mark.asyncio
async def test_generic_interpreter_does_not_apply_jaf_finding_rules() -> None:
    """JAF interpreter 未登録の通用 schema では JAF finding rule を課さない。"""

    # JAF なら evidence 無しの field assessment は finding_evidence_missing だが、通用では受理する。
    validated = await ResultValidator(MemoryEvidenceLookup(frozenset())).validate(
        run_id=uuid4(),
        schema={"type": "object", "additionalProperties": True},
        schema_ref=SCHEMA_CHECKSUM,
        structured_output={"field_assessments": [{"field_id": "x", "evidence_refs": []}]},
    )

    assert validated.confidence is None
    assert validated.needs_review is False


def _outcome(**overrides: object) -> dict[str, object]:
    """通用 OutcomeEnvelope の最小成功値を返す。"""

    value: dict[str, object] = {
        "outcome_version": "projectmind.outcome-envelope/v1",
        "summary": "Repository review complete",
        "status": "COMPLETED",
        "deliverables": [
            {
                "key": "report",
                "kind": "report",
                "title": "Review report",
                "content": "One authorization issue was found.",
            }
        ],
        "findings": [],
        "evidence_refs": ["ev_generic_1"],
        "artifact_refs": [],
        "open_questions": [],
        "limitations": [],
        "confidence": 0.9,
        "needs_review": False,
        "change_proposal_refs": [],
        "effects": [],
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_outcome_envelope_succeeds_without_task_specific_schema() -> None:
    """業務 output Schema が無い Run も通用包絡と owned Evidence だけで成功できる。"""

    compiled = compile_outcome_schema(None, task_schema_checksum=None)
    validated = await _validator(frozenset({"ev_generic_1"})).validate(
        run_id=uuid4(),
        schema=compiled.schema,
        schema_ref=compiled.checksum,
        structured_output=_outcome(),
        result_kind="OUTCOME_ENVELOPE",
    )

    assert validated.result_kind == "OUTCOME_ENVELOPE"
    assert validated.summary == "Repository review complete"
    assert validated.validation["outcome_envelope_valid"] is True
    assert validated.validation["task_schema_valid"] is None


@pytest.mark.asyncio
async def test_outcome_envelope_validates_optional_structured_data_separately() -> None:
    """宣言済み業務 Schema は structured_data だけへ追加適用する。"""

    task_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviewed_files"],
        "properties": {"reviewed_files": {"type": "integer", "minimum": 0}},
    }
    compiled = compile_outcome_schema(task_schema, task_schema_checksum=SCHEMA_CHECKSUM)
    validated = await _validator(frozenset({"ev_generic_1"})).validate(
        run_id=uuid4(),
        schema=compiled.schema,
        schema_ref=compiled.checksum,
        structured_output=_outcome(structured_data={"reviewed_files": 12}),
        result_kind="OUTCOME_ENVELOPE",
        task_schema=task_schema,
        task_schema_ref=SCHEMA_CHECKSUM,
    )

    assert validated.validation["task_schema_valid"] is True
    assert validated.validation["task_schema_ref"] == SCHEMA_CHECKSUM


@pytest.mark.asyncio
async def test_outcome_rejects_undeclared_structured_data_and_sensitive_fields() -> None:
    """未宣言業務 payload と credential らしい field は永続化前に遮断する。"""

    compiled = compile_outcome_schema(None, task_schema_checksum=None)
    with pytest.raises(ResultValidationError) as undeclared:
        await _validator(frozenset({"ev_generic_1"})).validate(
            run_id=uuid4(),
            schema=compiled.schema,
            schema_ref=compiled.checksum,
            structured_output=_outcome(structured_data={"value": 1}),
            result_kind="OUTCOME_ENVELOPE",
        )
    assert undeclared.value.code == "task_result_schema_undeclared"

    task_schema = {"type": "object", "additionalProperties": True}
    with_task = compile_outcome_schema(task_schema, task_schema_checksum=SCHEMA_CHECKSUM)
    with pytest.raises(ResultValidationError) as sensitive:
        await _validator(frozenset({"ev_generic_1"})).validate(
            run_id=uuid4(),
            schema=with_task.schema,
            schema_ref=with_task.checksum,
            structured_output=_outcome(structured_data={"access_token": "not-persisted"}),
            result_kind="OUTCOME_ENVELOPE",
            task_schema=task_schema,
            task_schema_ref=SCHEMA_CHECKSUM,
        )
    assert sensitive.value.code == "result_sensitive_field"
