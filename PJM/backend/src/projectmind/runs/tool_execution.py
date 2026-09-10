"""Tool audit の短い transaction に共有 Run lease と取消 gate を適用する。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import Run, RunAttempt, RunSegment
from projectmind.runs.domain import (
    ClaimedRun,
    LeaseValidationError,
    RunCancellationRequestedError,
    RunSegmentStatus,
    RunStatus,
)
from projectmind.runs.repository_base import _RunRepositoryBase


class ToolExecutionGate(_RunRepositoryBase):
    """Run→Segment→Attempt の lock と token 照合は既存 repository と共用する。"""

    def __init__(
        self, session: AsyncSession, *, authority_check: Callable[[], None],
        clock: Callable[[], datetime],
    ) -> None:
        """原 scope の失効通知と、各待機後に読み直す時計だけを内部注入する。"""

        super().__init__(session)
        self._authority_check = authority_check
        self._clock = clock

    async def validate(
        self, claimed: ClaimedRun, locked: tuple[Run, RunSegment | None, RunAttempt],
    ) -> datetime:
        """取消 SELECT の待機後も新しい時刻で検証し、誤った権限を取消理由で隠さない。"""

        self._validate_current(claimed, locked)
        try:
            await self._reject_cancelled_execution(locked[0].id)
        except RunCancellationRequestedError:
            self._validate_current(claimed, locked)
            raise
        return self._validate_current(claimed, locked)

    def _validate_current(
        self, claimed: ClaimedRun, locked: tuple[Run, RunSegment | None, RunAttempt],
    ) -> datetime:
        """最後の lock 内確認であり、その後の COMMIT 自体の時間上限は保証しない。"""

        run, segment, attempt = locked
        self._authority_check()
        if (
            run.project_id != claimed.project_id or run.status != RunStatus.RUNNING.value
            or (segment is not None and segment.status != RunSegmentStatus.RUNNING.value)
        ):
            raise LeaseValidationError("Run is not available for Tool execution")
        now = self._clock()
        self._validate_claimed_lease(attempt, claimed, now=now)
        return now
