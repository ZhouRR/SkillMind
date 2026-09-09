"""Org 門禁内で文書参照を読む。Run/Worker の下位 lock を逆向きに取得しない。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import Run, TaskSchedule, TaskScheduleOccurrence
from projectmind.documents.domain import DocumentInUseError
from projectmind.documents.references import (
    choice_document_ids,
    occurrence_document_ids,
    references_unavailable,
    run_document_ids,
)


class DocumentReferenceRepository:
    """原会話・Project・文書を锁定した同一 transaction からのみ呼び出す参照門禁。"""

    def __init__(self, session: AsyncSession) -> None:
        """新規 Run と調度保存が共有する Org lock の保持 session を受け取る。"""

        self._session = session

    async def require_unreferenced(self, *, project_id: UUID, document_id: UUID) -> None:
        """参照または未確認の歴史があれば拒否し、現在の状態/同名/副本で解除しない。"""

        runs = await self._session.scalars(select(Run).where(Run.project_id == project_id))
        for run in runs:
            try:
                identifiers = run_document_ids(run)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise references_unavailable() from error
            self._require_absent(document_id, identifiers)
        schedules = await self._session.scalars(
            select(TaskSchedule).where(TaskSchedule.project_id == project_id)
        )
        for schedule in schedules:
            try:
                identifiers = choice_document_ids(schedule.sources_json)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise references_unavailable() from error
            self._require_absent(document_id, identifiers)
        # OR + outer join で壊れた親子の所属を消さない。片側がこの Project なら照合対象とする。
        occurrences = await self._session.execute(
            select(TaskScheduleOccurrence, TaskSchedule)
            .outerjoin(TaskSchedule, TaskSchedule.id == TaskScheduleOccurrence.schedule_id)
            .where(
                or_(
                    TaskScheduleOccurrence.project_id == project_id,
                    TaskSchedule.project_id == project_id,
                )
            )
        )
        for occurrence, schedule in occurrences:
            try:
                identifiers = occurrence_document_ids(occurrence, schedule)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise references_unavailable() from error
            self._require_absent(document_id, identifiers)

    @staticmethod
    def _require_absent(document_id: UUID, identifiers: frozenset[UUID]) -> None:
        """存在する参照の ID や本文は例外に含めない。"""

        if document_id in identifiers:
            raise DocumentInUseError(
                "Document is referenced by retained execution or schedule data"
            )
