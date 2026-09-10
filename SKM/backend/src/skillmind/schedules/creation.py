"""持久 occurrence を普通 Run 作成 transaction へ参加させる。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.cancellation import check_pending_cancellation
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.creation_participation import RunCreationAuthority
from skillmind.runs.creation_request import TaskRunIntent
from skillmind.runs.domain import CreatedRun
from skillmind.schedules.domain import (
    ClaimedSchedule,
    ScheduleClaimLostError,
    ScheduleOutcome,
    ScheduleOverlapError,
    ScheduleOwnerUnavailableError,
    ScheduleTriggerResult,
)
from skillmind.schedules.repository import ScheduleRepository


class ScheduleRunCreationParticipant:
    """DB lock と認領だけを扱い、Run/資源/hash の生成は共通 service に任せる。"""

    def __init__(self, claim: ClaimedSchedule) -> None:
        """Worker が取得した一つの原認領を保持する。"""

        self._claim = claim

    async def authorize(
        self, session: AsyncSession, *, intent: TaskRunIntent, idempotency_key: str
    ) -> RunCreationAuthority:
        """Org→User→Project/Member→Schedule→Occurrence の順で現在の権限を固定する。"""

        await check_pending_cancellation()
        repository = ScheduleRepository(session)
        user = await repository.lock_creator(self._claim)
        if user is None or user.status != "ACTIVE" or user.system_role not in {"ADMIN", "USER"}:
            raise ScheduleOwnerUnavailableError("Schedule owner is unavailable")
        projects = ProjectRepository(session)
        try:
            access = await projects.lock_write_access(user=user, project_id=self._claim.project_id)
            projects.require_active_write_access(access)
        except (ProjectNotFoundError, ProjectArchivedError) as error:
            raise ScheduleOwnerUnavailableError(
                "Schedule owner has no active project access"
            ) from error
        locked = await repository.lock_claim(self._claim)
        repository.validate_original_request(locked, intent=intent, idempotency_key=idempotency_key)
        await check_pending_cancellation()
        return RunCreationAuthority(
            actor_system_role=user.system_role,
            project_membership="ADMIN_BYPASS" if user.system_role == "ADMIN" else "ACTIVE",
        )

    async def before_create(self, session: AsyncSession) -> None:
        """元 Run の不存在確認後だけ、同 Schedule の重複と現在の fence を検査する。"""

        repository = ScheduleRepository(session)
        locked = await repository.lock_claim(self._claim)
        if locked.occurrence.status != "PENDING":
            raise ScheduleClaimLostError("Settled occurrence cannot create another Run")
        if await repository.has_overlapping_run(locked):
            raise ScheduleOverlapError("Another Run from this schedule is still active")
        # 重複照会の待機中に期限が過ぎても、新規作成へ進めない。
        await repository.lock_claim(self._claim)
        await check_pending_cancellation()

    async def complete(self, session: AsyncSession, created: CreatedRun) -> None:
        """初期 Run と関連/計数を同時 commit し、応答喪失でも重複結算しない。"""

        await check_pending_cancellation()
        await ScheduleRepository(session).record_outcome(
            self._claim,
            ScheduleTriggerResult(
                schedule_id=self._claim.schedule_id,
                occurrence_at=self._claim.occurrence_at,
                outcome=ScheduleOutcome.RUN_CREATED,
                run_id=created.run_id,
            ),
        )
        await check_pending_cancellation()
