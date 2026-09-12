"""非同期解釈台帳の読み取り、固定 lock と明示投影を所有する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import SkillInterpretationCall, SkillInterpretationRequest
from skillmind.skills.interpretation_requests import (
    InterpretationRequestNotFoundError,
    InterpretationRequestSnapshot,
)


class InterpretationRequestRepository:
    """Org/User/Session の後に要求、次に呼出しを固定する。"""

    def __init__(self, session: AsyncSession) -> None:
        """呼出元が transaction を所有する Session を使う。"""

        self.session = session

    async def find(
        self, request_id: UUID, *, lock: bool = False
    ) -> SkillInterpretationRequest | None:
        """主キーで読み、lock 時は identity map の古い状態を再利用しない。"""

        statement = select(SkillInterpretationRequest).where(
            SkillInterpretationRequest.id == request_id
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        row: SkillInterpretationRequest | None = await self.session.scalar(statement)
        return row

    async def require(self, request_id: UUID, *, lock: bool = False) -> SkillInterpretationRequest:
        """旧 Queue や未知 UUID に原要求を補造しない。"""

        row = await self.find(request_id, lock=lock)
        if row is None:
            raise InterpretationRequestNotFoundError("Interpretation request was not found")
        return row

    async def find_execution(
        self, *, organization_id: UUID, execution_key: str
    ) -> SkillInterpretationRequest | None:
        """組織 gate の保持中に、同じ内容へ別の原要求を作る競争を閉じる。"""

        row: SkillInterpretationRequest | None = await self.session.scalar(
            select(SkillInterpretationRequest)
            .where(
                SkillInterpretationRequest.organization_id == organization_id,
                SkillInterpretationRequest.execution_key == execution_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return row

    async def calls(self, request_id: UUID) -> tuple[SkillInterpretationCall, ...]:
        """要求 lock の保持中に、初回から順に呼出しを固定する。"""

        return tuple(
            (
                await self.session.scalars(
                    select(SkillInterpretationCall)
                    .where(SkillInterpretationCall.request_id == request_id)
                    .order_by(SkillInterpretationCall.ordinal)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def stale_running(self, *, before: datetime, limit: int) -> tuple[UUID, ...]:
        """候補 ID だけを収集し、状態変更時は原資格から正しい lock 順で再取得する。"""

        result = await self.session.scalars(
            select(SkillInterpretationRequest.id)
            .where(
                SkillInterpretationRequest.status == "RUNNING",
                SkillInterpretationRequest.claimed_at <= before,
            )
            .order_by(SkillInterpretationRequest.claimed_at, SkillInterpretationRequest.id)
            .limit(limit)
        )
        return tuple(result.all())

    @staticmethod
    def snapshot(row: SkillInterpretationRequest) -> InterpretationRequestSnapshot:
        """変更可能な JSON を複製し、owner hash や ORM を持ち出さない。"""

        return InterpretationRequestSnapshot(
            request_id=row.id,
            organization_id=row.organization_id,
            actor_id=row.actor_id,
            auth_session_id=row.auth_session_id,
            skill_source_id=row.skill_source_id,
            execution_key=row.execution_key,
            input_checksum=row.input_checksum,
            input=deepcopy(row.input_json),
            status=row.status,
            created_at=row.created_at,
            interpretation_id=row.interpretation_id,
            error_code=row.error_code,
        )
