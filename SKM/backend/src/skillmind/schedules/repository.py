"""TaskSchedule の定義・状態 CAS と永続 occurrence の入口を提供する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from skillmind.auth.service import AuthenticatedActor
from skillmind.db.models import TaskSchedule, TaskScheduleOccurrence, User
from skillmind.schedules.domain import (
    TERMINAL_SCHEDULE_STATUSES,
    CreateScheduleCommand,
    ScheduleConflictError,
    ScheduleInvalidError,
    ScheduleKind,
    ScheduleOutcome,
    SchedulePage,
    ScheduleRecord,
    ScheduleStatus,
    UpdateScheduleCommand,
    plan_schedule_transition,
)
from skillmind.schedules.occurrence import positive_integer, utc_time
from skillmind.schedules.repository_occurrences import ScheduleOccurrenceRepository, _bump


class ScheduleRepository(ScheduleOccurrenceRepository):
    """短い transaction 内で最新定義を検証し、公開摘要と原監査を混同しない。"""

    async def create(self, command: CreateScheduleCommand) -> ScheduleRecord:
        """新 writer が検証した行だけ明示的に occurrence protocol 1 へ参加させる。"""

        now = datetime.now(UTC)
        row = TaskSchedule(
            id=uuid4(),
            project_id=command.project_id,
            name=command.name,
            kind=command.definition.kind.value,
            status=ScheduleStatus.ACTIVE.value,
            timezone=command.definition.timezone,
            cron_expression=command.definition.cron_expression,
            run_at=command.definition.run_at,
            end_at=command.definition.end_at,
            max_runs=command.definition.max_runs,
            skill_version_id=command.skill_version_id,
            task_key=command.task_key,
            input_json=deepcopy(command.input_json),
            sources_json=dict(command.sources),
            next_run_at=utc_time(command.next_run_at, minute=True),
            last_run_at=None,
            last_run_id=None,
            last_outcome=None,
            last_error=None,
            run_count=0,
            missed_count=0,
            created_by=command.created_by,
            row_version=1,
            configuration_version=1,
            occurrence_protocol=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return _to_record(row)

    async def get(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleRecord:
        """Project 境界内の schedule を取得し、越境と不存在を同じ例外へ畳む。"""

        return _to_record(await self._require_row(project_id=project_id, schedule_id=schedule_id))

    async def lock_schedule(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleRecord:
        """変更判断の前に最新行を固定し、service が待機後の原会話を再検証できるようにする。"""

        return _to_record(
            await self._require_row(project_id=project_id, schedule_id=schedule_id, lock=True)
        )

    async def list_for_project(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        q: str | None = None,
        status: ScheduleStatus | None = None,
    ) -> SchedulePage:
        """同じ絞込で件数とページを返し、tick に動かされない作成順を固定する。"""

        filters: list[ColumnElement[bool]] = [TaskSchedule.project_id == project_id]
        search = q.strip() if q is not None else ""
        if search:
            # 入力の %/_ と escape 文字を literal にし、検索語を pattern として実行しない。
            filters.append(
                or_(
                    TaskSchedule.name.icontains(search, autoescape=True),
                    TaskSchedule.task_key.icontains(search, autoescape=True),
                )
            )
        if status is not None:
            filters.append(TaskSchedule.status == status.value)

        total = int(
            await self._session.scalar(
                select(func.count()).select_from(TaskSchedule).where(*filters)
            )
            or 0
        )
        rows = await self._session.scalars(
            select(TaskSchedule)
            .where(*filters)
            .order_by(
                TaskSchedule.created_at.desc(),
                TaskSchedule.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return SchedulePage(tuple(_to_record(row) for row in rows), total, limit, offset)

    async def update_definition(self, command: UpdateScheduleCommand) -> ScheduleRecord:
        """lock 後の CAS・終態・残枠を確認し、認領済みの原設定は書き換えない。"""

        row = await self._require_row(
            project_id=command.project_id, schedule_id=command.schedule_id, lock=True
        )
        _version(row, command.expected_row_version)
        if ScheduleStatus(row.status) in TERMINAL_SCHEDULE_STATUSES:
            raise ScheduleInvalidError("Terminal schedule cannot be edited")
        pending = await self._pending_count(row.id)
        if command.definition.max_runs is not None:
            positive_integer(command.definition.max_runs, name="max_runs")
            if command.definition.max_runs < row.run_count + pending:
                raise ScheduleInvalidError(
                    "Schedule limit cannot be below created and pending runs"
                )
        positive_integer(row.configuration_version + 1, name="configuration_version")
        row.name = command.name
        row.kind = command.definition.kind.value
        row.timezone = command.definition.timezone
        row.cron_expression = command.definition.cron_expression
        row.run_at = command.definition.run_at
        row.end_at = command.definition.end_at
        row.max_runs = command.definition.max_runs
        row.input_json = deepcopy(command.input_json)
        row.sources_json = dict(command.sources)
        # PAUSED/ERROR を編集しただけで次回発火があるように見せない。
        row.next_run_at = (
            utc_time(command.next_run_at, minute=True) if row.status == "ACTIVE" else None
        )
        row.configuration_version += 1
        _bump(row, now=datetime.now(UTC))
        await self._session.flush()
        return _to_record(row)

    async def set_status(
        self,
        *,
        project_id: UUID,
        schedule_id: UUID,
        status: ScheduleStatus,
        next_run_at: datetime | None,
        expected_row_version: int,
        last_error: str | None = None,
    ) -> ScheduleRecord:
        """読取後の遷移判断を信用せず、現在の世代と状態機を lock 内で検証する。"""

        row = await self._require_row(project_id=project_id, schedule_id=schedule_id, lock=True)
        _version(row, expected_row_version)
        plan_schedule_transition(current=ScheduleStatus(row.status), target=status)
        if status is ScheduleStatus.ACTIVE:
            if row.occurrence_protocol != 1:
                raise ScheduleInvalidError(
                    "Legacy schedule cannot be activated without explicit migration"
                )
            if next_run_at is None:
                raise ScheduleInvalidError("Active schedule requires a future occurrence")
            pending = await self._pending_count(row.id)
            if row.max_runs is not None and row.run_count >= row.max_runs:
                raise ScheduleInvalidError("Schedule run limit is already exhausted")
            if row.max_runs is not None and row.run_count + pending > row.max_runs:
                raise ScheduleInvalidError("Schedule limit is below created and pending runs")
            row.next_run_at = utc_time(next_run_at, minute=True)
        else:
            row.next_run_at = None
        row.status = status.value
        row.last_error = last_error
        _bump(row, now=datetime.now(UTC))
        await self._session.flush()
        return _to_record(row)

    async def list_due(self, *, now: datetime, limit: int) -> list[ScheduleRecord]:
        """候補読取は認領権を与えず、claim 内で状態と原世代を再確認する。"""

        rows = await self._session.scalars(
            select(TaskSchedule)
            .where(
                TaskSchedule.status == "ACTIVE",
                TaskSchedule.next_run_at.is_not(None),
                TaskSchedule.next_run_at <= utc_time(now),
                ~select(TaskScheduleOccurrence.id)
                .where(
                    TaskScheduleOccurrence.schedule_id == TaskSchedule.id,
                    TaskScheduleOccurrence.status == "PENDING",
                )
                .exists(),
            )
            .order_by(TaskSchedule.next_run_at)
            .limit(limit)
        )
        return [_to_record(row) for row in rows]

    async def load_creator_actor(self, user_id: UUID) -> AuthenticatedActor | None:
        """外側の準備用 identity を読む。作成権限は transaction 内で別途再確認する。"""

        row = await self._session.scalar(
            select(User).where(User.id == user_id, User.status == "ACTIVE")
        )
        if row is None:
            return None
        return AuthenticatedActor(
            user_id=row.id,
            organization_id=row.organization_id,
            email=row.email,
            display_name=row.display_name,
            system_role=row.system_role,
        )


def _version(row: TaskSchedule, expected: int) -> None:
    """bool と同値な整数を CAS として扱わない。"""

    positive_integer(expected, name="expected_row_version")
    if row.row_version != expected:
        raise ScheduleConflictError("Schedule was modified by another request")


def _to_record(row: TaskSchedule) -> ScheduleRecord:
    """公開摘要と内部 protocol/version を明示的に投影する。"""

    return ScheduleRecord(
        schedule_id=row.id,
        project_id=row.project_id,
        name=row.name,
        kind=ScheduleKind(row.kind),
        status=ScheduleStatus(row.status),
        timezone=row.timezone,
        cron_expression=row.cron_expression,
        run_at=row.run_at,
        end_at=row.end_at,
        max_runs=row.max_runs,
        skill_version_id=row.skill_version_id,
        task_key=row.task_key,
        input_json=deepcopy(row.input_json),
        sources=dict(row.sources_json),
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_run_id=row.last_run_id,
        last_outcome=ScheduleOutcome(row.last_outcome) if row.last_outcome else None,
        last_error=row.last_error,
        run_count=row.run_count,
        missed_count=row.missed_count,
        created_by=row.created_by,
        row_version=row.row_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        configuration_version=row.configuration_version,
        occurrence_protocol=row.occurrence_protocol,
    )
