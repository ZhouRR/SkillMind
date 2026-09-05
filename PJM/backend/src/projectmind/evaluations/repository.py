"""PostgreSQL 上の追加式 Evaluation 永続化を実装する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import Evaluation, Run, RunResult
from projectmind.evaluations.domain import (
    CreateEvaluationCommand,
    EvaluationResultNotFoundError,
    EvaluationVerdict,
    InvalidEvaluationRevisionError,
    StoredEvaluation,
    StoredEvaluationRevision,
    resolve_json_pointer,
)
from projectmind.runs.domain import RunNotFoundError


class EvaluationRepository:
    """一つの transaction 内で Result 所有権確認と Evaluation 追加を行う。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def create(self, command: CreateEvaluationCommand) -> StoredEvaluation:
        """Project-scoped Result の原値を固定し、一件の Evaluation を追加する。"""

        result = await self._get_result(command.project_id, command.run_id)
        if not 1 <= command.rating <= 5:
            raise ValueError("Evaluation rating must be between 1 and 5")
        pointers = [revision.pointer for revision in command.revisions]
        if len(pointers) != len(set(pointers)):
            raise InvalidEvaluationRevisionError("Revision pointers must be unique")

        revisions = tuple(
            StoredEvaluationRevision(
                pointer=revision.pointer,
                original_value=deepcopy(resolve_json_pointer(result.data_json, revision.pointer)),
                suggested_value=deepcopy(revision.suggested_value),
                reason=revision.reason,
            )
            for revision in command.revisions
        )
        now = datetime.now(UTC)
        evaluation = Evaluation(
            id=uuid4(),
            result_id=result.id,
            user_id=command.user_id,
            rating=command.rating,
            verdict=command.verdict.value,
            comment=command.comment,
            revision_json=[
                {
                    "pointer": revision.pointer,
                    "original_value": revision.original_value,
                    "suggested_value": revision.suggested_value,
                    "reason": revision.reason,
                }
                for revision in revisions
            ],
            created_at=now,
        )
        self._session.add(evaluation)
        return self._to_stored(evaluation, run_id=command.run_id)

    async def list_for_run(
        self, *, project_id: UUID, run_id: UUID
    ) -> tuple[StoredEvaluation, ...]:
        """Project ownership を確認して Result の Evaluation を追加順で返す。"""

        result = await self._get_result(project_id, run_id)
        statement = (
            select(Evaluation)
            .where(Evaluation.result_id == result.id)
            .order_by(Evaluation.created_at, Evaluation.id)
        )
        evaluations = (await self._session.scalars(statement)).all()
        return tuple(self._to_stored(item, run_id=run_id) for item in evaluations)

    async def _get_result(self, project_id: UUID, run_id: UUID) -> RunResult:
        """Cross-Project の存在を隠し、同一 Run の不変 Result だけを返す。"""

        owned_run = await self._session.scalar(
            select(Run.id).where(Run.id == run_id, Run.project_id == project_id)
        )
        if owned_run is None:
            raise RunNotFoundError(f"Run not found in project: {run_id}")
        result = await self._session.scalar(select(RunResult).where(RunResult.run_id == run_id))
        if result is None:
            raise EvaluationResultNotFoundError(f"Run has no evaluable Result: {run_id}")
        return result

    @staticmethod
    def _to_stored(evaluation: Evaluation, *, run_id: UUID) -> StoredEvaluation:
        """ORM row を API から安全に返せる不変 DTO へ変換する。"""

        return StoredEvaluation(
            evaluation_id=evaluation.id,
            result_id=evaluation.result_id,
            run_id=run_id,
            user_id=evaluation.user_id,
            rating=evaluation.rating,
            verdict=EvaluationVerdict(evaluation.verdict),
            comment=evaluation.comment,
            revisions=tuple(
                StoredEvaluationRevision(
                    pointer=str(revision["pointer"]),
                    original_value=revision.get("original_value"),
                    suggested_value=revision.get("suggested_value"),
                    reason=str(revision["reason"]),
                )
                for revision in evaluation.revision_json
            ),
            created_at=evaluation.created_at,
        )
