"""Evaluation の transaction 境界を持つ application service を提供する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.evaluations.domain import CreateEvaluationCommand, StoredEvaluation
from projectmind.evaluations.repository import EvaluationRepository


class EvaluationService:
    """人工 Evaluation の追加と履歴取得 use case を実行する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def create(self, command: CreateEvaluationCommand) -> StoredEvaluation:
        """一 transaction で Result ownership を検証し Evaluation を追加する。"""

        async with self._session_factory() as session, session.begin():
            return await EvaluationRepository(session).create(command)

    async def list_for_run(
        self, *, project_id: UUID, run_id: UUID
    ) -> tuple[StoredEvaluation, ...]:
        """Project-scoped Run の人工評価履歴を read transaction で返す。"""

        async with self._session_factory() as session:
            return await EvaluationRepository(session).list_for_run(
                project_id=project_id,
                run_id=run_id,
            )
