"""凍結 Result Schema、全参照位置と保存済み effect 記録を主/子で共通検証する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.domain import RunContext
from skillmind.agent.outcome import OUTCOME_ENVELOPE_SCHEMA
from skillmind.agent.result_references import EffectSummaryLookup, collect_result_references
from skillmind.artifacts.domain import ArtifactIntegrityError
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.core.redaction import find_sensitive_key
from skillmind.db.models import ChangeProposal, Evidence


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


class ArtifactLookup(Protocol):
    """同 Run/原 Tool の保存済み byte まで検証する、主/子共用の read port。"""

    async def verified_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """帰属・回执・実際の size/hash が一致する参照だけを返す。"""

        ...


class PostgresArtifactLookup:
    """Download/checkpoint と同じ repository で保存時の Artifact 完全性を検証する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """実行 lease でなく読取 session の生成元を保持する。"""

        self._session_factory = session_factory

    async def verified_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """元の workspace を再読込せず、不変 snapshot を有界に照合する。"""

        if not refs:
            return frozenset()
        async with self._session_factory() as session:
            return await ArtifactRepository(session).verified_refs(run_id, refs)


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
                ChangeProposal.status.in_({"PENDING_APPROVAL", "APPROVED", "APPLYING"}),
            )
            return frozenset(await session.scalars(statement))


@dataclass(frozen=True, slots=True)
class ValidatedResult:
    """原候補から分離され、Schema と参照規則を通過した Result payload。"""

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
    """任意 output schema に対する既定解釈。業務語彙や固有 field には依存しない。

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

    Schema・参照所有・保存済み effect の整合性を共通検証する。business path 固有の
    判定や遠端の再検証は行わず、Artifact の保存事実を ID 外形から推測しない。
    """

    def __init__(
        self,
        evidence_lookup: EvidenceLookup,
        proposal_lookup: ProposalLookup | None = None,
        *,
        effect_lookup: EffectSummaryLookup | None = None,
        artifact_lookup: ArtifactLookup | None = None,
    ) -> None:
        """参照/効果の読取 port と通用 interpreter を保持し、未装配は成功へ降格しない。"""

        self._evidence_lookup = evidence_lookup
        self._proposal_lookup = proposal_lookup
        self._effect_lookup = effect_lookup
        self._artifact_lookup = artifact_lookup
        self._generic = GenericResultInterpreter()

    async def validate_context(
        self, context: RunContext, *, structured_output: Any
    ) -> ValidatedResult:
        """凍結 context の出力契約を主/子で同じ規則により解決して検証する。"""

        schema_ref = context.task_snapshot.get("output_schema_checksum")
        if not isinstance(schema_ref, str):
            schema_ref = context.task_snapshot.get("output_schema")
        if not isinstance(schema_ref, str):
            schema_ref = "inline://run-result-schema"
        task_schema = context.task_snapshot.get("task_output_schema_json")
        task_schema_ref = context.task_snapshot.get("task_output_schema_checksum")
        return await self.validate(
            run_id=context.run_id,
            schema=context.result_schema,
            schema_ref=schema_ref,
            structured_output=structured_output,
            result_kind=(
                "OUTCOME_ENVELOPE"
                if context.task_snapshot.get("result_kind") == "OUTCOME_ENVELOPE"
                else "STRUCTURED_OUTPUT"
            ),
            task_schema=task_schema if isinstance(task_schema, dict) else None,
            task_schema_ref=task_schema_ref if isinstance(task_schema_ref, str) else None,
        )

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
        # Lookup の await 中に producer が nested JSON を変えても、検証済み候補を差し替えさせない。
        structured_output = deepcopy(structured_output)
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

        references = collect_result_references(
            structured_output, outcome=result_kind == "OUTCOME_ENVELOPE",
        )
        refs = references.evidence
        existing = await self._evidence_lookup.existing_refs(run_id, refs)
        missing = refs - existing
        if missing:
            raise ResultValidationError(
                "evidence_reference_invalid",
                f"Agent result referenced {len(missing)} unavailable Evidence item(s)",
            )

        artifact_refs = references.artifacts
        if artifact_refs:
            if self._artifact_lookup is None:
                raise ResultValidationError(
                    "artifact_reference_unavailable",
                    "Agent result referenced Artifact items without a verifiable saved snapshot",
                )
            try:
                verified = await self._artifact_lookup.verified_refs(run_id, artifact_refs)
            except ArtifactIntegrityError:
                raise ResultValidationError(
                    "artifact_reference_invalid", "Agent result Artifact snapshot is invalid",
                ) from None
            if artifact_refs - verified:
                raise ResultValidationError(
                    "artifact_reference_invalid",
                    "Agent result referenced unavailable Artifact items",
                )
        change_proposal_refs = references.proposals
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
            incomplete = await self._proposal_lookup.incomplete_refs(run_id, change_proposal_refs)
            if incomplete:
                raise ResultValidationError(
                    "change_proposal_incomplete",
                    "Agent result referenced a ChangeProposal that is still awaiting "
                    "an effect decision",
                )

        if references.effects and (
            self._effect_lookup is None
            or await self._effect_lookup.invalid_refs(run_id, references.effects)
        ):
            raise ResultValidationError(
                "effect_summary_invalid",
                "Agent result effect claims did not match the saved platform records",
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
                "artifact_refs_valid": True,
                "change_proposal_count": len(change_proposal_refs),
                "change_proposal_refs_valid": True,
                "reference_checks": {
                    "version": "skillmind.result-reference-checks/v2",
                    "evidence": "RUN_OWNERSHIP",
                    "proposals": "RUN_OWNERSHIP_AND_STATE",
                    "effects": (
                        "PLATFORM_RECORD_MATCH" if result_kind == "OUTCOME_ENVELOPE"
                        else "NOT_APPLICABLE"
                    ),
                    "artifacts": "RUN_OWNERSHIP_AND_CONTENT",
                },
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
