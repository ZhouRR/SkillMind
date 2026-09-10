"""PostgreSQL 上の追加式 Evaluation 永続化を実装する。"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import Evaluation, Run, RunResult
from projectmind.evaluations.domain import (
    CreateEvaluationCommand,
    EvaluationError,
    EvaluationIntegrityError,
    EvaluationResultMismatchError,
    EvaluationResultNotFoundError,
    EvaluationRevisionProposal,
    EvaluationSubmissionConflictError,
    EvaluationSubmissionNotFoundError,
    EvaluationVerdict,
    InvalidEvaluationCommandError,
    InvalidEvaluationCursorError,
    StoredEvaluation,
    StoredEvaluationPage,
    StoredEvaluationRevision,
    StoredEvaluationSubmission,
    evaluation_request_hash,
    require_evaluation_uuid,
    resolve_json_pointer,
    strict_evaluation_json,
    validate_evaluation_command,
)
from projectmind.runs.domain import RunNotFoundError


class EvaluationRepository:
    """一つの transaction 内で Result 所有権確認と Evaluation 追加を行う。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def create(self, command: CreateEvaluationCommand) -> StoredEvaluation:
        """Project-scoped Result の原値を固定し、一件の Evaluation を追加する。"""

        command = deepcopy(command)
        validate_evaluation_command(command)
        result = await self._get_result(command.project_id, command.run_id)
        return self._append(command, result=result, submission_key=None)

    async def submit(
        self,
        command: CreateEvaluationCommand,
        *,
        submission_key: UUID,
        result_id: UUID,
    ) -> StoredEvaluationSubmission:
        """同じ actor/Result/key を原内容と照合し、重放で新しい行を追加しない。"""

        command = deepcopy(command)
        require_evaluation_uuid(submission_key)
        request_hash = evaluation_request_hash(
            command,
            result_id=result_id,
            submission_key=submission_key,
        )
        result = await self._get_result(command.project_id, command.run_id, result_id=result_id)
        original = await self._find_submission(result.id, command.user_id, submission_key)
        if original is not None:
            stored = self._to_stored(original, project_id=command.project_id, result=result)
            if original.request_hash != request_hash:
                raise EvaluationSubmissionConflictError("Evaluation submission content conflicts")
            return StoredEvaluationSubmission(
                command.project_id,
                command.run_id,
                submission_key,
                stored,
                True,
            )
        stored = self._append(command, result=result, submission_key=submission_key)
        return StoredEvaluationSubmission(
            command.project_id,
            command.run_id,
            submission_key,
            stored,
        )

    async def get_submission(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        user_id: UUID,
        submission_key: UUID,
        result_id: UUID,
    ) -> StoredEvaluationSubmission:
        """現在同一 actor の原要求だけを確認し、未命中で追加や修復を行わない。"""

        for identity in (project_id, run_id, user_id, submission_key, result_id):
            require_evaluation_uuid(identity)
        result = await self._get_result(project_id, run_id, result_id=result_id)
        original = await self._find_submission(result.id, user_id, submission_key)
        if original is None:
            raise EvaluationSubmissionNotFoundError("Evaluation submission was not found")
        return StoredEvaluationSubmission(
            project_id,
            run_id,
            submission_key,
            self._to_stored(original, project_id=project_id, result=result),
            True,
        )

    async def list_for_run(self, *, project_id: UUID, run_id: UUID) -> tuple[StoredEvaluation, ...]:
        """Project ownership を確認して Result の Evaluation を追加順で返す。"""

        result = await self._get_result(project_id, run_id)
        statement = (
            select(Evaluation)
            .where(Evaluation.result_id == result.id)
            .order_by(Evaluation.created_at, Evaluation.id)
        )
        evaluations = (await self._session.scalars(statement)).all()
        return tuple(
            self._to_stored(item, project_id=project_id, result=result) for item in evaluations
        )

    async def list_page(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        limit: int = 20,
        after: UUID | None = None,
    ) -> StoredEvaluationPage:
        """同 Result の anchor を検証し、SQL limit+1 だけで次ページの有無を求める。"""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidEvaluationCommandError("Evaluation page limit is invalid")
        if after is not None and (not isinstance(after, UUID) or after.int == 0):
            raise InvalidEvaluationCursorError("Evaluation cursor is invalid")
        result = await self._get_result(project_id, run_id)
        statement = select(Evaluation).where(Evaluation.result_id == result.id)
        if after is not None:
            anchor = await self._session.scalar(
                select(Evaluation).where(Evaluation.id == after, Evaluation.result_id == result.id),
            )
            if anchor is None:
                raise InvalidEvaluationCursorError("Evaluation cursor is invalid")
            self._to_stored(anchor, project_id=project_id, result=result)
            statement = statement.where(
                or_(
                    Evaluation.created_at > anchor.created_at,
                    and_(Evaluation.created_at == anchor.created_at, Evaluation.id > anchor.id),
                )
            )
        rows = (
            await self._session.scalars(
                statement.order_by(Evaluation.created_at, Evaluation.id).limit(limit + 1),
            )
        ).all()
        checked = tuple(self._to_stored(row, project_id=project_id, result=result) for row in rows)
        items = checked[:limit]
        return StoredEvaluationPage(
            project_id,
            run_id,
            result.id,
            items,
            items[-1].evaluation_id if len(checked) > limit else None,
        )

    async def _get_result(
        self,
        project_id: UUID,
        run_id: UUID,
        *,
        result_id: UUID | None = None,
    ) -> RunResult:
        """Cross-Project の存在を隠し、同一 Run の不変 Result だけを返す。"""

        owned_run = await self._session.scalar(
            select(Run.id)
            .where(Run.id == run_id, Run.project_id == project_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if owned_run is None:
            raise RunNotFoundError("Run not found in project")
        result = await self._session.scalar(
            select(RunResult)
            .where(RunResult.run_id == run_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True),
        )
        if result is None:
            raise EvaluationResultNotFoundError("Run has no evaluable Result")
        if result_id is not None and result.id != result_id:
            raise EvaluationResultMismatchError("Evaluation Result does not match")
        return result

    async def _find_submission(
        self,
        result_id: UUID,
        user_id: UUID,
        submission_key: UUID,
    ) -> Evaluation | None:
        """原要求を完全な三元 identity で検索し、他 actor の履歴へ fallback しない。"""

        original: Evaluation | None = await self._session.scalar(
            select(Evaluation)
            .where(
                Evaluation.result_id == result_id,
                Evaluation.user_id == user_id,
                Evaluation.submission_key == submission_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True),
        )
        return original

    def _append(
        self,
        command: CreateEvaluationCommand,
        *,
        result: RunResult,
        submission_key: UUID | None,
    ) -> StoredEvaluation:
        """修訂原値を同じ Result から保存し、DTO と ORM の可変 JSON を共有しない。"""

        revisions = [
            {
                "pointer": revision.pointer,
                "original_value": deepcopy(
                    resolve_json_pointer(result.data_json, revision.pointer)
                ),
                "suggested_value": deepcopy(revision.suggested_value),
                "reason": revision.reason,
            }
            for revision in command.revisions
        ]
        evaluation = Evaluation(
            id=uuid4(),
            result_id=result.id,
            user_id=command.user_id,
            rating=command.rating,
            verdict=command.verdict.value,
            comment=command.comment,
            revision_json=revisions,
            submission_key=submission_key,
            request_hash=(
                evaluation_request_hash(
                    command,
                    result_id=result.id,
                    submission_key=submission_key,
                )
                if submission_key is not None
                else None
            ),
            created_at=datetime.now(UTC),
        )
        stored = self._to_stored(evaluation, project_id=command.project_id, result=result)
        self._session.add(evaluation)
        return stored

    @staticmethod
    def _to_stored(
        evaluation: Evaluation,
        *,
        project_id: UUID,
        result: RunResult,
    ) -> StoredEvaluation:
        """保存形状・原値・新 hash を検証し、欠落を null や文字列へ変換しない。"""

        try:
            for identity in (evaluation.id, evaluation.result_id, evaluation.user_id):
                require_evaluation_uuid(identity)
            if evaluation.result_id != result.id:
                raise ValueError("Evaluation Result binding is invalid")
            if (
                not isinstance(evaluation.created_at, datetime)
                or evaluation.created_at.utcoffset() is None
            ):
                raise ValueError("Evaluation timestamp is invalid")
            if type(evaluation.revision_json) is not list:
                raise ValueError("Evaluation revisions are invalid")
            revisions: list[StoredEvaluationRevision] = []
            for item in evaluation.revision_json:
                if type(item) is not dict or set(item) != {
                    "pointer",
                    "original_value",
                    "suggested_value",
                    "reason",
                }:
                    raise ValueError("Evaluation revision shape is invalid")
                if type(item["pointer"]) is not str:
                    raise ValueError("Evaluation pointer is invalid")
                original = resolve_json_pointer(result.data_json, item["pointer"])
                if strict_evaluation_json(original) != strict_evaluation_json(
                    item["original_value"]
                ):
                    raise ValueError("Evaluation original value is invalid")
                revisions.append(
                    StoredEvaluationRevision(
                        item["pointer"],
                        deepcopy(item["original_value"]),
                        deepcopy(item["suggested_value"]),
                        item["reason"],
                    )
                )
            command = CreateEvaluationCommand(
                project_id,
                result.run_id,
                evaluation.user_id,
                evaluation.rating,
                EvaluationVerdict(evaluation.verdict),
                evaluation.comment,
                tuple(
                    EvaluationRevisionProposal(item.pointer, item.suggested_value, item.reason)
                    for item in revisions
                ),
            )
            validate_evaluation_command(command)
            if evaluation.submission_key is None:
                if evaluation.request_hash is not None:
                    raise ValueError("Evaluation request binding is invalid")
            else:
                require_evaluation_uuid(evaluation.submission_key)
                if (
                    type(evaluation.request_hash) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", evaluation.request_hash) is None
                    or evaluation.request_hash
                    != evaluation_request_hash(
                        command,
                        result_id=result.id,
                        submission_key=evaluation.submission_key,
                    )
                ):
                    raise ValueError("Evaluation request binding is invalid")
            return StoredEvaluation(
                evaluation.id,
                result.id,
                result.run_id,
                evaluation.user_id,
                evaluation.rating,
                command.verdict,
                evaluation.comment,
                tuple(revisions),
                evaluation.created_at,
            )
        except (EvaluationError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise EvaluationIntegrityError("Evaluation record is invalid") from error
