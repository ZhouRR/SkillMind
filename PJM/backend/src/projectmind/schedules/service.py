"""TaskSchedule の use case と到期発火を実装する (計画 §22)。

発火は最終的に即時実行と同一の `RunService.create_task_run` を呼ぶ。schedule が決めるのは
「いつ・誰の身分で・どの凍結設定で」開始するかだけで、Run 以降の闸门 (承認、事前許可、
binding 再検証) には一切触れない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.service import AuthenticatedActor
from projectmind.core.logging import log_event
from projectmind.projects.domain import ProjectNotFoundError, ProjectStatus
from projectmind.projects.repository import ProjectRepository
from projectmind.runs.domain import (
    TERMINAL_RUN_STATUSES,
    CreatedRun,
    IdempotencyConflictError,
    RunNotFoundError,
    TaskSourceSelectionError,
)
from projectmind.runs.repository import RunRepository
from projectmind.runs.service import RunService
from projectmind.schedules.cron import (
    CronExpressionError,
    next_occurrence,
    normalize_timezone,
    parse_cron,
    upcoming_occurrences,
)
from projectmind.schedules.domain import (
    DEFAULT_SCHEDULE_TICK_LIMIT,
    MAX_SCHEDULE_NAME_LENGTH,
    ClaimedSchedule,
    CreateScheduleCommand,
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleKind,
    ScheduleNotFoundError,
    ScheduleOutcome,
    SchedulePage,
    ScheduleRecord,
    ScheduleStatus,
    ScheduleTickReport,
    ScheduleTriggerResult,
    UpdateScheduleCommand,
    plan_schedule_transition,
    schedule_idempotency_key,
)
from projectmind.schedules.repository import ScheduleRepository
from projectmind.skills import PublishedTaskNotFoundError, SkillService, TaskInputInvalidError

logger = logging.getLogger(__name__)

# 保存前に提示する発火時刻の件数 (docs/07 §8.3 は「少なくとも三次」)。
PREVIEW_OCCURRENCE_COUNT = 5
# ONCE の予約が過去になっていないかを判定するときの許容差。時計のずれで保存が弾かれるのを防ぐ。
_PAST_TOLERANCE = timedelta(minutes=1)
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

    # ------------------------------------------------------------------ 読み取り

    async def list_schedules(self, *, project_id: UUID, limit: int, offset: int) -> SchedulePage:
        """Project 内 schedule を列挙する。"""

        async with self._session_factory() as session:
            return await ScheduleRepository(session).list_for_project(
                project_id=project_id, limit=limit, offset=offset
            )

    async def get_schedule(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleRecord:
        """Project 境界内の schedule を取得する。"""

        async with self._session_factory() as session:
            return await ScheduleRepository(session).get(
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
        actor: AuthenticatedActor,
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
        async with self._session_factory() as session, session.begin():
            return await ScheduleRepository(session).create(
                CreateScheduleCommand(
                    project_id=project_id,
                    name=validated_name,
                    definition=definition,
                    skill_version_id=skill_version_id,
                    task_key=task_key,
                    input_json=input_json,
                    sources=sources,
                    created_by=actor.user_id,
                    next_run_at=first,
                )
            )

    async def update_schedule(
        self,
        *,
        project_id: UUID,
        schedule_id: UUID,
        name: str,
        definition: ScheduleDefinition,
        input_json: dict[str, Any],
        sources: dict[str, str],
        expected_row_version: int,
    ) -> ScheduleRecord:
        """定義と凍結入力を差し替える。終態 schedule は編集させない。"""

        validated_name = _validate_name(name)
        first = _first_occurrence(definition)
        async with self._session_factory() as session, session.begin():
            repository = ScheduleRepository(session)
            current = await repository.get(project_id=project_id, schedule_id=schedule_id)
            if current.status in {ScheduleStatus.COMPLETED, ScheduleStatus.ARCHIVED}:
                raise ScheduleInvalidError("Schedule is no longer editable")
            await self._validate_task_configuration(
                project_id=project_id,
                skill_version_id=current.skill_version_id,
                task_key=current.task_key,
                input_json=input_json,
                sources=sources,
            )
            return await repository.update_definition(
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

    async def change_status(
        self, *, project_id: UUID, schedule_id: UUID, target: ScheduleStatus
    ) -> ScheduleRecord:
        """暂停/恢复/归档を状態機経由で適用する。

        ACTIVE へ戻すときは次回発火時刻を計算し直す。止まっている間に過ぎた分は追いかけない
        (§22 D6 と同じ理由——復帰の瞬間に溜まった回数だけ走らせない)。
        """

        async with self._session_factory() as session, session.begin():
            repository = ScheduleRepository(session)
            current = await repository.get(project_id=project_id, schedule_id=schedule_id)
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
            return await repository.set_status(
                project_id=project_id,
                schedule_id=schedule_id,
                status=target,
                next_run_at=next_run_at,
                last_error=None if target is ScheduleStatus.ACTIVE else current.last_error,
            )

    # ------------------------------------------------------------------ 発火

    async def run_due_schedules(
        self, *, now: datetime | None = None, limit: int = DEFAULT_SCHEDULE_TICK_LIMIT
    ) -> ScheduleTickReport:
        """到期 schedule を認領して発火する。Worker の cron tick から呼ぶ。"""

        reference = now or datetime.now(UTC)
        async with self._session_factory() as session:
            due = await ScheduleRepository(session).list_due(now=reference, limit=limit)
        results: list[ScheduleTriggerResult] = []
        for record in due:
            claimed = await self._claim(record, now=reference)
            if claimed is None:
                continue
            results.append(await self._fire(claimed))
        return ScheduleTickReport(results=tuple(results))

    async def _claim(self, record: ScheduleRecord, *, now: datetime) -> ClaimedSchedule | None:
        """`next_run_at` の CAS で一件を獲得し、同時に次回候補へ進める (§22 D6/D7)。"""

        occurrence = record.next_run_at
        if occurrence is None:
            return None
        plan = plan_occurrence(
            _definition_of(record),
            occurrence=occurrence,
            now=now,
            run_count=record.run_count,
        )
        async with self._session_factory() as session, session.begin():
            claimed = await ScheduleRepository(session).claim(
                schedule_id=record.schedule_id,
                expected_next_run_at=occurrence,
                next_run_at=None if plan.exhausted else plan.next_run_at,
                missed=plan.missed,
            )
        if not claimed:
            return None
        return ClaimedSchedule(
            schedule_id=record.schedule_id,
            project_id=record.project_id,
            occurrence_at=occurrence,
            skill_version_id=record.skill_version_id,
            task_key=record.task_key,
            input_json=record.input_json,
            sources=record.sources,
            created_by=record.created_by,
            exhausted=plan.exhausted,
            missed=plan.missed,
        )

    async def _fire(self, claimed: ClaimedSchedule) -> ScheduleTriggerResult:
        """一件の発火を実行し、結果を schedule 行へ書き戻す。"""

        result = await self._create_scheduled_run(claimed)
        status = _status_after(result, exhausted=claimed.exhausted)
        async with self._session_factory() as session, session.begin():
            await ScheduleRepository(session).record_outcome(
                schedule_id=claimed.schedule_id,
                occurrence_at=claimed.occurrence_at,
                outcome=result.outcome,
                run_id=result.run_id,
                detail=result.detail,
                status=status,
            )
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
        find_replay = partial(
            self._run_service.find_task_run_replay,
            project_id=claimed.project_id,
            skill_version_id=claimed.skill_version_id,
            task_key=claimed.task_key,
            input_json=claimed.input_json,
            sources=claimed.sources,
            actor_id=actor.user_id,
            idempotency_key=idempotency_key,
        )
        try:
            replay = await find_replay()
        except (TaskSourceSelectionError, IdempotencyConflictError) as error:
            return _failed(claimed, str(error))
        if replay is not None:
            # 自分が既に作った Run は「他の実行との重複」ではない。現在の version や
            # resource を再解決せず元の occurrence の結果へ収斂させる。
            return _created_run_result(claimed, replay)
        overlap = await self._overlapping_run(claimed)
        if overlap is not None:
            try:
                replay = await find_replay()
            except (TaskSourceSelectionError, IdempotencyConflictError) as error:
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
            except (TaskSourceSelectionError, IdempotencyConflictError) as replay_error:
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
                actor_system_role=actor.system_role,
                project_membership=("ADMIN_BYPASS" if actor.system_role == "ADMIN" else "ACTIVE"),
            )
        except (TaskSourceSelectionError, IdempotencyConflictError) as error:
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
        """直前の Run がまだ非終態なら、その状態名を返す (§22 D5)。"""

        async with self._session_factory() as session:
            record = await ScheduleRepository(session).get(
                project_id=claimed.project_id, schedule_id=claimed.schedule_id
            )
            if record.last_run_id is None:
                return None
            try:
                run = await RunRepository(session).get(record.last_run_id)
            except RunNotFoundError:
                return None
        if run.status in TERMINAL_RUN_STATUSES:
            return None
        return run.status.value

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


def build_definition(
    *,
    kind: str,
    timezone: str,
    cron_expression: str | None,
    run_at: datetime | None,
    end_at: datetime | None,
    max_runs: int | None,
    now: datetime | None = None,
) -> ScheduleDefinition:
    """入力から検証済みの `ScheduleDefinition` を作る。API と service が同じ検証を通す。"""

    reference = now or datetime.now(UTC)
    try:
        schedule_kind = ScheduleKind(kind)
    except ValueError as error:
        raise ScheduleInvalidError("Schedule kind is not supported") from error
    try:
        zone = normalize_timezone(timezone)
    except CronExpressionError as error:
        raise ScheduleInvalidError(str(error)) from error
    if schedule_kind is ScheduleKind.CRON:
        if not cron_expression:
            raise ScheduleInvalidError("Cron schedule requires an expression")
        if run_at is not None:
            raise ScheduleInvalidError("Cron schedule must not fix a single instant")
        try:
            parse_cron(cron_expression)
        except CronExpressionError as error:
            raise ScheduleInvalidError(str(error)) from error
        normalized_cron: str | None = " ".join(cron_expression.split())
        normalized_run_at: datetime | None = None
    else:
        if run_at is None:
            raise ScheduleInvalidError("One-shot schedule requires an instant")
        if cron_expression:
            raise ScheduleInvalidError("One-shot schedule must not carry an expression")
        normalized_run_at = run_at.astimezone(UTC).replace(second=0, microsecond=0)
        if normalized_run_at < reference - _PAST_TOLERANCE:
            raise ScheduleInvalidError("One-shot schedule is in the past")
        normalized_cron = None
    if max_runs is not None and max_runs < 1:
        raise ScheduleInvalidError("Schedule run limit must be positive")
    normalized_end = end_at.astimezone(UTC) if end_at is not None else None
    if normalized_end is not None and normalized_end <= reference:
        raise ScheduleInvalidError("Schedule end is already in the past")
    return ScheduleDefinition(
        kind=schedule_kind,
        timezone=zone,
        cron_expression=normalized_cron,
        run_at=normalized_run_at,
        end_at=normalized_end,
        max_runs=max_runs,
    )


@dataclass(frozen=True, slots=True)
class OccurrencePlan:
    """一回の発火を認領するときに決まること。"""

    next_run_at: datetime | None
    missed: int
    exhausted: bool


def plan_occurrence(
    definition: ScheduleDefinition,
    *,
    occurrence: datetime,
    now: datetime,
    run_count: int,
) -> OccurrencePlan:
    """認領時の「次回候補・見送り回数・打ち切りか」を決める純関数 (§22 D6/D9)。

    停止中に過ぎた分は数えるだけで走らせない。次回候補を「今より後の最初の一致」に取ることで、
    復帰の瞬間に溜まった回数だけ Run が並ぶ状態を構造的に避ける。
    """

    following = _next_from_definition(definition, after=now)
    missed = _count_missed(definition, start=occurrence, until=now)
    run_count_after = run_count + 1
    exhausted = (
        definition.kind is ScheduleKind.ONCE
        or following is None
        or (definition.max_runs is not None and run_count_after >= definition.max_runs)
        or (definition.end_at is not None and following > definition.end_at)
    )
    return OccurrencePlan(next_run_at=following, missed=missed, exhausted=exhausted)


def _occurrences(definition: ScheduleDefinition, *, after: datetime, count: int) -> list[datetime]:
    """定義が生む発火時刻を最大 `count` 件、`end_at` で打ち切って返す。"""

    if definition.kind is ScheduleKind.ONCE:
        instant = definition.run_at
        if instant is None or instant <= after:
            return []
        if definition.end_at is not None and instant > definition.end_at:
            return []
        return [instant]
    expression = parse_cron(definition.cron_expression or "")
    limit = count
    if definition.max_runs is not None:
        limit = min(limit, definition.max_runs)
    occurrences = upcoming_occurrences(
        expression, after=after, timezone=definition.timezone, count=limit
    )
    if definition.end_at is not None:
        occurrences = [item for item in occurrences if item <= definition.end_at]
    return occurrences


def _first_occurrence(definition: ScheduleDefinition) -> datetime:
    """保存に必要な最初の発火時刻。存在しなければ保存させない。"""

    occurrences = _occurrences(definition, after=datetime.now(UTC), count=1)
    if not occurrences:
        raise ScheduleInvalidError("Schedule has no future occurrence")
    return occurrences[0]


def _next_from_definition(definition: ScheduleDefinition, *, after: datetime) -> datetime | None:
    """次の発火時刻。尽きていれば None。"""

    occurrences = _occurrences(definition, after=after, count=1)
    return occurrences[0] if occurrences else None


def _count_missed(definition: ScheduleDefinition, *, start: datetime, until: datetime) -> int:
    """`start` の次から `until` までに過ぎた発火回数を数える (§22 D6 の記録用)。

    追いかけないと決めた回数そのものなので、監査のために残す。数え上げも有界にする。
    """

    if definition.kind is ScheduleKind.ONCE:
        return 0
    expression = parse_cron(definition.cron_expression or "")
    missed = 0
    cursor = start
    while missed < 1000:
        upcoming = next_occurrence(expression, after=cursor, timezone=definition.timezone)
        if upcoming is None or upcoming > until:
            return missed
        missed += 1
        cursor = upcoming
    return missed


def _definition_of(record: ScheduleRecord) -> ScheduleDefinition:
    """保存済み行から発火定義を復元する。"""

    return ScheduleDefinition(
        kind=record.kind,
        timezone=record.timezone,
        cron_expression=record.cron_expression,
        run_at=record.run_at,
        end_at=record.end_at,
        max_runs=record.max_runs,
    )


def _status_after(result: ScheduleTriggerResult, *, exhausted: bool) -> ScheduleStatus | None:
    """発火結果から次の status を決める。変えるべきでないときは None。"""

    if result.outcome is ScheduleOutcome.FAILED_PRECONDITION:
        return ScheduleStatus.ERROR
    if exhausted:
        return ScheduleStatus.COMPLETED
    return None


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


def _validate_name(name: str) -> str:
    """一覧に出す短い名前だけを受け付ける。"""

    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_SCHEDULE_NAME_LENGTH:
        raise ScheduleInvalidError("Schedule name is invalid")
    return cleaned


__all__ = [
    "PREVIEW_OCCURRENCE_COUNT",
    "OccurrencePlan",
    "ScheduleNotFoundError",
    "ScheduleService",
    "build_definition",
    "plan_occurrence",
]
