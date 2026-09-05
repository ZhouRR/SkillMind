"""Structured Result の Schema、Evidence、M0 business rule を検証する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.agent.outcome import OUTCOME_ENVELOPE_SCHEMA
from projectmind.core.redaction import find_sensitive_key
from projectmind.db.models import ChangeProposal, Evidence


class ResultValidationError(ValueError):
    """Run を成功終態へ進めてはならない安定した検証 error。"""

    def __init__(self, code: str, message: str) -> None:
        """監査へ保存可能な code と機密を含まない message を保持する。"""

        super().__init__(message)
        self.code = code
        self.message = message


class EvidenceLookup(Protocol):
    """ResultValidator と Evidence storage を分離する read port。"""

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """指定 Run に属する Evidence reference だけを返す。"""

        ...


class PostgresEvidenceLookup:
    """PostgreSQL の追加式 Evidence index から Run 所有 reference を取得する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Read transaction 用 session factory を保持する。"""

        self._session_factory = session_factory

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """空集合を短絡し、Run ID と reference の両方で絞り込む。"""

        if not refs:
            return frozenset()
        async with self._session_factory() as session:
            statement = select(Evidence.evidence_ref).where(
                Evidence.run_id == run_id,
                Evidence.evidence_ref.in_(refs),
            )
            return frozenset(await session.scalars(statement))


class ProposalLookup(Protocol):
    """OutcomeEnvelope の ChangeProposal reference 所有/終状態を確認する port。"""

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """指定 Run に属する Proposal reference だけを返す。"""

        ...

    async def incomplete_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """まだ批准/apply 待機中の Proposal reference を返す。"""

        ...


class PostgresProposalLookup:
    """PostgreSQL の ChangeProposal index から ownership と lifecycle を確認する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Read transaction 用 session factory を保持する。"""

        self._session_factory = session_factory

    async def existing_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """Run/ref の複合条件に一致する Proposal reference を返す。"""

        if not refs:
            return frozenset()
        async with self._session_factory() as session:
            statement = select(ChangeProposal.proposal_ref).where(
                ChangeProposal.run_id == run_id,
                ChangeProposal.proposal_ref.in_(refs),
            )
            return frozenset(await session.scalars(statement))

    async def incomplete_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """User/effect 待機 status の Proposal reference を返す。"""

        if not refs:
            return frozenset()
        async with self._session_factory() as session:
            statement = select(ChangeProposal.proposal_ref).where(
                ChangeProposal.run_id == run_id,
                ChangeProposal.proposal_ref.in_(refs),
                ChangeProposal.status.in_(
                    {"PENDING_APPROVAL", "APPROVED", "APPLYING"}
                ),
            )
            return frozenset(await session.scalars(statement))


@dataclass(frozen=True, slots=True)
class ValidatedResult:
    """Schema と Evidence rule を通過した immutable Result payload。"""

    data: dict[str, Any]
    result_kind: str
    evidence_refs: frozenset[str]
    artifact_refs: frozenset[str]
    change_proposal_refs: frozenset[str]
    summary: str
    confidence: float | None
    needs_review: bool
    validation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResultInterpretation:
    """Output schema 固有の結果解釈 (表示 summary、集約 confidence、要確認フラグ)。"""

    summary: str
    confidence: float | None
    needs_review: bool


class GenericResultInterpreter:
    """任意 output schema に対する既定解釈。JAF 語彙や固有 field には依存しない。

    summary/confidence/needs_review を同名の通用 top-level field から読み取り、無ければ中立な
    既定値へ畳む。追加の finding rule は課さない (Schema/Evidence 所有は ResultValidator が保証)。
    """

    def validate_findings(self, structured_output: Mapping[str, Any]) -> None:
        """通用結果には schema 固有の追加 finding rule を課さない。"""

        return None

    def interpret(self, structured_output: Mapping[str, Any]) -> ResultInterpretation:
        """通用 convention の summary/confidence/needs_review を導出する。"""

        summary = structured_output.get("summary")
        summary_text = (
            summary.strip()
            if isinstance(summary, str) and summary.strip()
            else "Structured result recorded"
        )
        confidence = structured_output.get("confidence")
        confidence_value = (
            float(confidence)
            if isinstance(confidence, int | float)
            and not isinstance(confidence, bool)
            and 0.0 <= float(confidence) <= 1.0
            else None
        )
        needs_review = structured_output.get("needs_review")
        return ResultInterpretation(
            summary=summary_text[:280],
            confidence=confidence_value,
            needs_review=needs_review if isinstance(needs_review, bool) else False,
        )


class ResultValidator:
    """SDK structured_output を Platform の成功条件で再検証する。

    Schema 妥当性と Evidence 所有という通用不変条件だけを保証し、business path や task key に
    よる追加 rule は持たない。
    """

    def __init__(
        self,
        evidence_lookup: EvidenceLookup,
        proposal_lookup: ProposalLookup | None = None,
    ) -> None:
        """Evidence 所有確認 port と通用 result interpreter を保持する。"""

        self._evidence_lookup = evidence_lookup
        self._proposal_lookup = proposal_lookup
        self._generic = GenericResultInterpreter()

    async def validate(
        self,
        *,
        run_id: UUID,
        schema: Mapping[str, Any],
        schema_ref: str,
        structured_output: Any,
        result_kind: str = "STRUCTURED_OUTPUT",
        task_schema: Mapping[str, Any] | None = None,
        task_schema_ref: str | None = None,
    ) -> ValidatedResult:
        """通用包絡、任意業務 Schema、機密遮断と Evidence 所有を順に検証する。"""

        Draft202012Validator.check_schema(schema)
        if not isinstance(structured_output, dict):
            raise ResultValidationError(
                "structured_output_missing",
                "Agent result did not contain a JSON object",
            )
        validation: dict[str, Any] = {
            "schema_ref": schema_ref,
            "schema_valid": True,
        }
        if result_kind == "OUTCOME_ENVELOPE":
            self._validate_schema(
                schema=OUTCOME_ENVELOPE_SCHEMA,
                value=structured_output,
                code="outcome_envelope_invalid",
                label="OutcomeEnvelope",
            )
            validation["outcome_envelope_valid"] = True
            validation["outcome_version"] = structured_output["outcome_version"]
            if task_schema is not None:
                structured_data = structured_output.get("structured_data")
                self._validate_schema(
                    schema=task_schema,
                    value=structured_data,
                    code="task_result_schema_invalid",
                    label="task-specific result",
                )
                validation["task_schema_valid"] = True
                validation["task_schema_ref"] = task_schema_ref
            else:
                if "structured_data" in structured_output:
                    raise ResultValidationError(
                        "task_result_schema_undeclared",
                        "Agent result included structured_data without a task-specific Schema",
                    )
                validation["task_schema_valid"] = None
                validation["task_schema_ref"] = None
        self._validate_schema(
            schema=schema,
            value=structured_output,
            code="result_schema_invalid",
            label="output",
        )
        if find_sensitive_key(structured_output) is not None:
            raise ResultValidationError(
                "result_sensitive_field",
                "Agent result contained a sensitive field",
            )

        refs = frozenset(_collect_evidence_refs(structured_output))
        existing = await self._evidence_lookup.existing_refs(run_id, refs)
        missing = refs - existing
        if missing:
            raise ResultValidationError(
                "evidence_reference_invalid",
                f"Agent result referenced {len(missing)} unavailable Evidence item(s)",
            )

        artifact_refs = _string_refs(structured_output.get("artifact_refs"))
        change_proposal_refs = _string_refs(structured_output.get("change_proposal_refs"))
        if change_proposal_refs:
            if self._proposal_lookup is None:
                raise ResultValidationError(
                    "change_proposal_reference_invalid",
                    "Agent result referenced unavailable ChangeProposal items",
                )
            existing_proposals = await self._proposal_lookup.existing_refs(
                run_id, change_proposal_refs
            )
            missing_proposals = change_proposal_refs - existing_proposals
            if missing_proposals:
                raise ResultValidationError(
                    "change_proposal_reference_invalid",
                    "Agent result referenced "
                    f"{len(missing_proposals)} unavailable ChangeProposal item(s)",
                )
            incomplete = await self._proposal_lookup.incomplete_refs(
                run_id, change_proposal_refs
            )
            if incomplete:
                raise ResultValidationError(
                    "change_proposal_incomplete",
                    "Agent result referenced a ChangeProposal that is still awaiting "
                    "an effect decision",
                )

        # 表示用 convention も全 task で同じ interpreter を通し、business path 分岐を作らない。
        self._generic.validate_findings(structured_output)
        interpretation = self._generic.interpret(structured_output)
        return ValidatedResult(
            data=dict(structured_output),
            result_kind=result_kind,
            evidence_refs=refs,
            artifact_refs=artifact_refs,
            change_proposal_refs=change_proposal_refs,
            summary=interpretation.summary,
            confidence=interpretation.confidence,
            needs_review=interpretation.needs_review,
            validation={
                **validation,
                "evidence_refs_valid": True,
                "evidence_count": len(refs),
                "artifact_count": len(artifact_refs),
                "change_proposal_count": len(change_proposal_refs),
                "change_proposal_refs_valid": True,
            },
        )

    @staticmethod
    def _validate_schema(
        *,
        schema: Mapping[str, Any],
        value: Any,
        code: str,
        label: str,
    ) -> None:
        """値を Schema へ照合し、instance 内容を含まない安定 error に変換する。"""

        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            path = "/".join(str(part) for part in errors[0].absolute_path) or "$"
            raise ResultValidationError(
                code,
                f"Agent result did not match {label} Schema at {path}",
            )


def _collect_evidence_refs(value: Any) -> set[str]:
    """Result tree の source_ref/evidence_refs だけを再帰的に収集する。"""

    refs: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key == "source_ref" and isinstance(nested, str):
                refs.add(nested)
            elif key == "evidence_refs" and isinstance(nested, list):
                refs.update(item for item in nested if isinstance(item, str))
            else:
                refs.update(_collect_evidence_refs(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            refs.update(_collect_evidence_refs(nested))
    return refs


def _string_refs(value: Any) -> frozenset[str]:
    """通用包絡の参照 list から string だけを immutable 集合へ変換する。"""

    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))
