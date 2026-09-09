"""TaskSchedule の use case と到期発火を実装する (計画 §22)。

発火は最終的に即時実行と同一の `RunService.create_task_run` を呼ぶ。schedule が決めるのは
「いつ・誰の身分で・どの凍結設定で」開始するかだけで、Run 以降の闸门 (承認、事前許可、
binding 再検証) には一切触れない。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.service import AuthenticatedActor
from projectmind.core.cancellation import check_pending_cancellation
from projectmind.core.logging import log_event
from projectmind.projects.domain import ProjectNotFoundError, ProjectStatus
from projectmind.projects.repository import ProjectRepository
from projectmind.runs.domain import (
    CreatedRun,
    IdempotencyConflictError,
    TaskSourceSelectionError,
)
from projectmind.runs.service import RunService
from projectmind.schedules.creation import ScheduleRunCreationParticipant
from projectmind.schedules.domain import (
    DEFAULT_SCHEDULE_TICK_LIMIT,
    ClaimedSchedule,
    CreateScheduleCommand,
    ScheduleActivity,
    ScheduleClaimLostError,
    ScheduleConflictError,
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleNotFoundError,
    ScheduleOccurrenceConflictError,
    ScheduleOutcome,
    ScheduleOverlapError,
    ScheduleOwnerUnavailableError,
    SchedulePage,
    ScheduleRecord,
    ScheduleStatus,
    ScheduleTickReport,
    ScheduleTriggerResult,
    UpdateScheduleCommand,
    plan_schedule_transition,
    schedule_idempotency_key,
)
from projectmind.schedules.planning import (
    PREVIEW_OCCURRENCE_COUNT,
    OccurrencePlan,
    build_definition,
    plan_occurrence,
)
from projectmind.schedules.planning import (
    definition_of as _definition_of,
)
from projectmind.schedules.planning import (
    first_occurrence as _first_occurrence,
)
from projectmind.schedules.planning import (
    next_from_definition as _next_from_definition,
)
from projectmind.schedules.planning import (
    occurrences as _occurrences,
)
from projectmind.schedules.planning import (
    validate_name as _validate_name,
)
from projectmind.schedules.repository import ScheduleRepository
from projectmind.skills import PublishedTaskNotFoundError, SkillService, TaskInputInvalidError
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import LockedUsers, UserRepository

logger = logging.getLogger(__name__)

# 失敗理由は一覧に出す短い説明。Ticket 本文や接続情報を載せないため上限を切る。
_MAX_DETAIL_LENGTH = 500


class ScheduleService:
    """Transaction 境界を所有して schedule use case を実行する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        skill_service: SkillService,
        run_service: RunService,
    ) -> None:
        """永続 resource と、発火時に再利用する既存 use case を保持する。"""

        self._session_factory = session_factory
        self._skill_service = skill_service
        self._run_service = run_service
        self._worker_id = f"schedule-{uuid4()}"

    # ------------------------------------------------------------------ 読み取り

    async def list_schedules(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        q: str | None = None,
        status: ScheduleStatus | None = None,
    ) -> SchedulePage:
        """Project 内 schedule を列挙する。"""

        async with self._session_factory() as session:
            return await ScheduleRepository(session).list_for_project(
                project_id=project_id,
                limit=limit,
                offset=offset,
                q=q,
                status=status,
            )

    async def get_schedule(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleRecord:
        """Project 境界内の schedule を取得する。"""

        async with self._session_factory() as session:
            return await ScheduleRepository(session).get(
                project_id=project_id, schedule_id=schedule_id
            )

    async def get_activity(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleActivity:
        """在途の読取だけを行い、作成者再認可・発火・lease 更新を呼び出さない。"""

        async with self._session_factory() as session:
            return await ScheduleRepository(session).get_activity(
                project_id=project_id, schedule_id=schedule_id
            )

    def preview(
        self, definition: ScheduleDefinition, *, now: datetime | None = None
    ) -> list[datetime]:
        """定義を検証し、次の発火時刻を最大 `PREVIEW_OCCURRENCE_COUNT` 件返す。

        保存経路と同じ検証を通すので、preview が空なら保存も通らない。
        """

        reference = now or datetime.now(UTC)
        return _occurrences(definition, after=reference, count=PREVIEW_OCCURRENCE_COUNT)

    # ------------------------------------------------------------------ 書き込み

    async def create_schedule(
        self,
        *,
        project_id: UUID,
        access: UserAccess,
        name: str,
        definition: ScheduleDefinition,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, Any],
        sources: dict[str, str],
    ) -> ScheduleRecord:
        """定義・task・資源選択をすべて検証してから schedule を作成する。"""

        validated_name = _validate_name(name)
        first = _first_occurrence(definition)
        await self._validate_task_configuration(
            project_id=project_id,
            skill_version_id=skill_version_id,
            task_key=task_key,
            input_json=input_json,
            sources=sources,
        )
        async with self._write_transaction(access, project_id=project_id) as (repository, users, _):
            result = await repository.create(
                CreateScheduleCommand(
                    project_id=project_id,
                    name=validated_name,
                    definition=definition,
                    skill_version_id=skill_version_id,
                    task_key=task_key,
                    input_json=input_json,
                    sources=sources,
                    created_by=users.actor.id,
                    next_run_at=first,
                )
            )
        return result

    async def update_schedule(
        self,
        *,
        project_id: UUID,
        schedule_id: UUID,
        access: UserAccess,
        name: str,
        definition: ScheduleDefinition,
        input_json: dict[str, Any],
        sources: dict[str, str],
        expected_row_version: int,
    ) -> ScheduleRecord:
        """定義と凍結入力を差し替える。終態 schedule は編集させない。"""

        validated_name = _validate_name(name)
        first = _first_occurrence(definition)
        async with self._session_factory() as session:
            current = await ScheduleRepository(session).get(
                project_id=project_id, schedule_id=schedule_id
            )
        # 旧草稿は現在の終態/入力エラーへ読み替えず、比較・再読取のため原版衝突を先に返す。
        if current.row_version != expected_row_version:
            raise ScheduleConflictError("Schedule was modified by another request")
        if current.status in {ScheduleStatus.COMPLETED, ScheduleStatus.ARCHIVED}:
            raise ScheduleInvalidError("Schedule is no longer editable")
        # Task 解決は row lock の外、版/状態/名額の最終判断は repository の同じ lock 内に置く。
        await self._validate_task_configuration(
            project_id=project_id,
            skill_version_id=current.skill_version_id,
            task_key=current.task_key,
            input_json=input_json,
            sources=sources,
        )
        async with self._write_transaction(
            access, project_id=project_id, schedule_id=schedule_id
        ) as (repository, _, _):
            result = await repository.update_definition(
                UpdateScheduleCommand(
                    schedule_id=schedule_id,
                    project_id=project_id,
                    name=validated_name,
                    definition=definition,
                    input_json=input_json,
                    sources=sources,
                    next_run_at=first,
                    expected_row_version=expected_row_version,
                )
            )
        return result

    async def change_status(
        self,
        *,
        project_id: UUID,
        schedule_id: UUID,
        access: UserAccess,
        target: ScheduleStatus,
        expected_row_version: int,
    ) -> ScheduleRecord:
        """暂停/恢复/归档を状態機経由で適用する。

        ACTIVE へ戻すときは次回発火時刻を計算し直す。止まっている間に過ぎた分は追いかけない
        (§22 D6 と同じ理由——復帰の瞬間に溜まった回数だけ走らせない)。
        """

        async with self._write_transaction(
            access, project_id=project_id, schedule_id=schedule_id
        ) as (repository, _, current):
            if current is None:
                raise RuntimeError("Schedule status change requires a locked schedule")
            # 利用者が見た版を最新読取で置換せず、旧画面の操作は遷移判断の前に拒否する。
            if current.row_version != expected_row_version:
                raise ScheduleConflictError("Schedule was modified by another request")
            plan_schedule_transition(current=current.status, target=target)
            next_run_at: datetime | None = None
            if target is ScheduleStatus.ACTIVE:
                next_run_at = _next_from_definition(
                    _definition_of(current), after=datetime.now(UTC)
                )
                if next_run_at is None:
                    raise ScheduleInvalidError(
                        "Schedule has no future occurrence and cannot be resumed"
                    )
            result = await repository.set_status(
                project_id=project_id,
                schedule_id=schedule_id,
                status=target,
                next_run_at=next_run_at,
                last_error=None if target is ScheduleStatus.ACTIVE else current.last_error,
                expected_row_version=expected_row_version,
            )
        return result

    @asynccontextmanager
    async def _write_transaction(
        self, access: UserAccess, *, project_id: UUID, schedule_id: UUID | None = None
    ) -> AsyncIterator[tuple[ScheduleRepository, LockedUsers, ScheduleRecord | None]]:
        """原会話と現在の Project 資格を短期 lock で固定し、全管理書込に同じ門禁を適用する。"""

        validate_user_access(access)
        async with self._session_factory() as session, session.begin():
            users = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
                read_only_actor=True,
            )
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects = ProjectRepository(session)
            project = await projects.lock_write_access(user=users.actor, project_id=project_id)
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects.require_active_write_access(project)
            repository = ScheduleRepository(session)
            current = None
            if schedule_id is not None:
                current = await repository.lock_schedule(
                    project_id=project_id, schedule_id=schedule_id
                )
                # Schedule の待機中に失効しても、状態/CAS 判断や書き込みへ進めない。
                authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
                projects.require_active_write_access(project)
            yield repository, users, current
            # flush は commit ではない。FK/名額の待機後も再検証し、例外は全変更を巻き戻す。
            await session.flush()
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects.require_active_write_access(project)

    # ------------------------------------------------------------------ 発火

    async def run_due_schedules(
        self, *, now: datetime | None = None, limit: int = DEFAULT_SCHEDULE_TICK_LIMIT
    ) -> ScheduleTickReport:
        """元の PENDING を先に回収し、残りの枠だけ新しい到期予定を認領する。"""

        if type(limit) is not int or limit < 1:
            raise ValueError("Schedule tick limit must be positive")
        results: list[ScheduleTriggerResult] = []
        attempts = 0
        while attempts < limit:
            await check_pending_cancellation()
            async with self._session_factory() as session, session.begin():
                recovered = await ScheduleRepository(session).claim_recoverable(
                    now=now or datetime.now(UTC),
                    limit=1,
                    worker_id=self._worker_id,
                    token=secrets.token_urlsafe(32),
                )
            if not recovered:
                break
            attempts += 1
            result = await self._try_fire(recovered[0])
            if result is not None:
                results.append(result)
        if attempts < limit:
            async with self._session_factory() as session:
                due = await ScheduleRepository(session).list_due(
                    now=now or datetime.now(UTC),
                    limit=limit - attempts,
                )
            for record in due:
                await check_pending_cancellation()
                claimed = await self._claim(record, now=now or datetime.now(UTC))
                if claimed is not None:
                    result = await self._try_fire(claimed)
                    if result is not None:
                        results.append(result)
        return ScheduleTickReport(results=tuple(results))

    async def _claim(self, record: ScheduleRecord, *, now: datetime) -> ClaimedSchedule | None:
        """版/候補/名額と原 snapshot を同じ認領 transaction で保存する。"""

        async with self._session_factory() as session, session.begin():
            return await ScheduleRepository(session).claim_due(
                record,
                now=now,
                worker_id=self._worker_id,
                token=secrets.token_urlsafe(32),
            )

    async def _try_fire(self, claimed: ClaimedSchedule) -> ScheduleTriggerResult | None:
        """基盤失敗/旧認領を業務失効と偽らず、原 PENDING の回復へ委ねる。"""

        try:
            return await self._fire(claimed)
        except (ScheduleClaimLostError, ScheduleOccurrenceConflictError):
            reason = "claim_unavailable"
        except (DBAPIError, DatabaseTimeoutError, OSError, TimeoutError):
            # commit の応答が失われても新しい key を作らず、次の tick が元の Run を照会する。
            reason = "persistence_unknown"
        await check_pending_cancellation()
        log_event(
            logger,
            logging.WARNING,
            "schedule.trigger.unconfirmed",
            schedule_id=str(claimed.schedule_id),
            project_id=str(claimed.project_id),
            outcome=reason,
        )
        return None

    async def _fire(self, claimed: ClaimedSchedule) -> ScheduleTriggerResult:
        """Run 関連は作成 transaction、既知の見送り/失効だけは独立に結算する。"""

        result = await self._create_scheduled_run(claimed)
        if result.outcome is not ScheduleOutcome.RUN_CREATED:
            async with self._session_factory() as session, session.begin():
                result = await ScheduleRepository(session).record_outcome(claimed, result)
        await check_pending_cancellation()
        log_event(
            logger,
            logging.WARNING
            if result.outcome is ScheduleOutcome.FAILED_PRECONDITION
            else logging.INFO,
            "schedule.trigger.completed",
            schedule_id=str(claimed.schedule_id),
            project_id=str(claimed.project_id),
            outcome=result.outcome.value,
            run_id=str(result.run_id) if result.run_id else None,
            missed=claimed.missed,
        )
        return result

    async def _create_scheduled_run(self, claimed: ClaimedSchedule) -> ScheduleTriggerResult:
        """前提を再検証してから Run を作る。失効はすべて FAILED_PRECONDITION に畳む。"""

        actor = await self._authorize_creator(claimed)
        if actor is None:
            return _failed(claimed, "Schedule owner no longer has access to this project")
        idempotency_key = schedule_idempotency_key(
            schedule_id=claimed.schedule_id, occurrence_at=claimed.occurrence_at
        )
        participant = ScheduleRunCreationParticipant(claimed)
        find_replay = partial(
            self._run_service.find_task_run_replay,
            project_id=claimed.project_id,
            skill_version_id=claimed.skill_version_id,
            task_key=claimed.task_key,
            input_json=claimed.input_json,
            sources=claimed.sources,
            actor_id=actor.user_id,
            idempotency_key=idempotency_key,
            authorization=participant,
        )
        try:
            replay = await find_replay()
        except (
            TaskSourceSelectionError,
            IdempotencyConflictError,
            ScheduleOwnerUnavailableError,
        ) as error:
            return _failed(claimed, str(error))
        if replay is not None:
            # 自分が既に作った Run は「他の実行との重複」ではない。現在の version や
            # resource を再解決せず元の occurrence の結果へ収斂させる。
            return _created_run_result(claimed, replay)
        overlap = await self._overlapping_run(claimed)
        if overlap is not None:
            try:
                replay = await find_replay()
            except (
                TaskSourceSelectionError,
                IdempotencyConflictError,
                ScheduleOwnerUnavailableError,
            ) as error:
                return _failed(claimed, str(error))
            if replay is not None:
                return _created_run_result(claimed, replay)
            return ScheduleTriggerResult(
                schedule_id=claimed.schedule_id,
                occurrence_at=claimed.occurrence_at,
                outcome=ScheduleOutcome.SKIPPED_OVERLAP,
                detail=f"Previous Run is still {overlap}",
            )
        try:
            resolved = await self._skill_service.resolve_task_run(
                project_id=claimed.project_id,
                skill_version_id=claimed.skill_version_id,
                task_key=claimed.task_key,
                input_json=claimed.input_json,
            )
        except (PublishedTaskNotFoundError, TaskInputInvalidError) as error:
            try:
                replay = await find_replay()
            except (
                TaskSourceSelectionError,
                IdempotencyConflictError,
                ScheduleOwnerUnavailableError,
            ) as replay_error:
                return _failed(claimed, str(replay_error))
            return (
                _created_run_result(claimed, replay)
                if replay is not None
                else _failed(claimed, str(error))
            )
        try:
            run = await self._run_service.create_task_run(
                project_id=claimed.project_id,
                resolved=resolved,
                input_json=claimed.input_json,
                sources=claimed.sources,
                idempotency_key=idempotency_key,
                trace_id=None,
                actor_id=actor.user_id,
                authorization=participant,
            )
        except ScheduleOverlapError:
            return ScheduleTriggerResult(
                schedule_id=claimed.schedule_id,
                occurrence_at=claimed.occurrence_at,
                outcome=ScheduleOutcome.SKIPPED_OVERLAP,
                detail="Another Run from this schedule is still active",
            )
        except (
            TaskSourceSelectionError,
            IdempotencyConflictError,
            ScheduleOwnerUnavailableError,
        ) as error:
            return _failed(claimed, str(error))
        return _created_run_result(claimed, run)

    async def _authorize_creator(self, claimed: ClaimedSchedule) -> AuthenticatedActor | None:
        """作成者が今も ACTIVE で、その Project へ到達できるかを再判定する (§22 D8)。"""

        async with self._session_factory() as session:
            actor = await ScheduleRepository(session).load_creator_actor(claimed.created_by)
            if actor is None:
                return None
            try:
                project = await ProjectRepository(session).get_accessible(
                    actor=actor, project_id=claimed.project_id
                )
            except ProjectNotFoundError:
                return None
        # 归档済み Project では即時実行も拒否される。調度だけが素通りする穴を作らない。
        if project.status is not ProjectStatus.ACTIVE:
            return None
        return actor

    async def _overlapping_run(self, claimed: ClaimedSchedule) -> str | None:
        """摘要指針でなく同じ Schedule に関連済みの全非終態 Run を確認する。"""

        async with self._session_factory() as session, session.begin():
            repository = ScheduleRepository(session)
            locked = await repository.lock_claim(claimed)
            return "non-terminal" if await repository.has_overlapping_run(locked) else None

    async def _validate_task_configuration(
        self,
        *,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, Any],
        sources: dict[str, str],
    ) -> None:
        """保存時点で task 解決と資源選択を本番と同じ経路で確認する。"""

        try:
            resolved = await self._skill_service.resolve_task_run(
                project_id=project_id,
                skill_version_id=skill_version_id,
                task_key=task_key,
                input_json=input_json,
            )
        except PublishedTaskNotFoundError as error:
            raise ScheduleInvalidError(str(error)) from error
        except TaskInputInvalidError as error:
            raise ScheduleInvalidError(str(error)) from error
        try:
            await self._run_service.validate_task_sources(
                project_id=project_id, resolved=resolved, sources=sources
            )
        except TaskSourceSelectionError as error:
            raise ScheduleInvalidError(str(error)) from error


def _created_run_result(claimed: ClaimedSchedule, run: CreatedRun) -> ScheduleTriggerResult:
    """初回作成と再送を同じ occurrence/Run 関連へ投影する。"""

    return ScheduleTriggerResult(
        schedule_id=claimed.schedule_id,
        occurrence_at=claimed.occurrence_at,
        outcome=ScheduleOutcome.RUN_CREATED,
        run_id=run.run_id,
    )


def _failed(claimed: ClaimedSchedule, detail: str) -> ScheduleTriggerResult:
    """前提失効の結果を組み立てる。説明は長さを切り詰める。"""

    return ScheduleTriggerResult(
        schedule_id=claimed.schedule_id,
        occurrence_at=claimed.occurrence_at,
        outcome=ScheduleOutcome.FAILED_PRECONDITION,
        detail=detail[:_MAX_DETAIL_LENGTH],
    )


__all__ = [
    "PREVIEW_OCCURRENCE_COUNT",
    "OccurrencePlan",
    "ScheduleNotFoundError",
    "ScheduleService",
    "build_definition",
    "plan_occurrence",
]
