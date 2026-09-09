"""実 ORM と明示 SQL fake で認領を検証し、実 DB の並行実行とは区別する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.db.models import Run, TaskSchedule, TaskScheduleOccurrence
from projectmind.runs.creation_request import TaskRunIntent
from projectmind.runs.domain import CreateRunCommand, derive_task_id, lease_token_hash, request_hash
from projectmind.schedules.domain import ClaimedSchedule, ScheduleDefinition, ScheduleKind
from projectmind.schedules.occurrence import ScheduleOccurrenceSnapshot
from projectmind.schedules.repository import ScheduleRepository
from projectmind.schedules.repository_occurrences import _claimed

NOW = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
TOKEN = "fixture-schedule-lease"


def schedule_row() -> TaskSchedule:
    """履歴を持たない protocol 1 の新規行を明示的に作る。"""

    return TaskSchedule(
        id=uuid4(),
        project_id=uuid4(),
        name="Fixture schedule",
        kind="CRON",
        status="ACTIVE",
        timezone="UTC",
        cron_expression="* * * * *",
        run_at=None,
        end_at=None,
        max_runs=2,
        skill_version_id=uuid4(),
        task_key="analyze",
        input_json={"value": 1},
        sources_json={},
        next_run_at=NOW,
        last_run_at=None,
        last_run_id=None,
        last_outcome=None,
        last_error=None,
        run_count=0,
        missed_count=0,
        created_by=uuid4(),
        row_version=1,
        configuration_version=1,
        occurrence_protocol=1,
        created_at=NOW,
        updated_at=NOW,
    )


def occurrence_row(schedule: TaskSchedule) -> TaskScheduleOccurrence:
    """原 snapshot codec を用い、別の test 専用保存形式を作らない。"""

    snapshot = ScheduleOccurrenceSnapshot(
        schedule_id=schedule.id,
        occurrence_at=NOW,
        name=schedule.name,
        configuration_version=schedule.configuration_version,
        claim_row_version=2,
        definition=ScheduleDefinition(ScheduleKind.CRON, "UTC", "* * * * *", None, None, 2),
        intent=TaskRunIntent(
            schedule.project_id,
            schedule.skill_version_id,
            schedule.task_key,
            schedule.created_by,
            schedule.input_json,
            schedule.sources_json,
        ),
        exhausted=False,
        missed=0,
    )
    return TaskScheduleOccurrence(
        id=uuid4(),
        schedule_id=schedule.id,
        project_id=schedule.project_id,
        created_by=schedule.created_by,
        skill_version_id=schedule.skill_version_id,
        occurrence_at=NOW,
        idempotency_key=snapshot.idempotency_key,
        configuration_version=schedule.configuration_version,
        snapshot_json=snapshot.to_json(),
        snapshot_checksum=snapshot.checksum,
        status="PENDING",
        worker_id="fixture-worker",
        lease_token_hash=lease_token_hash(TOKEN),
        lease_generation=1,
        lease_expires_at=NOW + timedelta(seconds=60),
        attempt_count=1,
        run_id=None,
        outcome=None,
        detail=None,
        settled_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def claimed_schedule() -> ClaimedSchedule:
    """adapter/service tests が共用する、永続形式と一致した原認領を返す。"""

    return _claimed(occurrence_row(schedule_row()), token=TOKEN)


def run_for_occurrence(row: TaskScheduleOccurrence, *, status: str = "QUEUED") -> Run:
    """普通 Run の原 request_hash を再利用した関連行を作る。"""

    snapshot = ScheduleOccurrenceSnapshot.from_json(
        row.snapshot_json, checksum=row.snapshot_checksum
    )
    intent = snapshot.intent
    command = CreateRunCommand(
        project_id=row.project_id,
        task_id=derive_task_id(skill_version_id=intent.skill_version_id, task_key=intent.task_key),
        idempotency_key=row.idempotency_key,
        input_json=intent.input_json,
        task_snapshot_json={
            "creation_request": intent.to_json(),
            "skill_version_id": str(intent.skill_version_id),
            "task_key": intent.task_key,
        },
        permission_snapshot_json={"actor_id": str(intent.actor_id)},
        selected_sources_json={},
        limits_snapshot_json={},
        trace_id=None,
    )
    return Run(
        id=uuid4(),
        project_id=command.project_id,
        task_id=command.task_id,
        idempotency_key=command.idempotency_key,
        request_hash=request_hash(command),
        status=status,
        row_version=1,
        input_json=command.input_json,
        task_snapshot_json=command.task_snapshot_json,
        permission_snapshot_json=command.permission_snapshot_json,
        selected_sources_json={},
        limits_snapshot_json={},
        created_at=NOW,
        updated_at=NOW,
    )


class ScheduleDatabase:
    """SQL の対象・条件を観測し、rollback の影響範囲を明示する局部 fake。"""

    def __init__(self, *, claimed: bool = False) -> None:
        """行を固定し、外部接続を一切持たない session double を組み立てる。"""

        self.schedule = schedule_row()
        self.schedules = [self.schedule]
        self.locked_schedule_ids: set[UUID] = set()
        self.occurrences: list[TaskScheduleOccurrence] = []
        self.runs: list[Run] = []
        self.statements: list[Any] = []
        self.session = MagicMock(spec=AsyncSession)
        self.session.scalar = AsyncMock(side_effect=self.scalar)
        self.session.scalars = AsyncMock(side_effect=self.scalars)
        self.session.flush = AsyncMock()
        self.session.add.side_effect = self.add
        self.repo = ScheduleRepository(self.session)
        if claimed:
            self.occurrences.append(occurrence_row(self.schedule))
            self.schedule.row_version = 2
            self.schedule.next_run_at = NOW + timedelta(minutes=1)

    @property
    def claim(self) -> ClaimedSchedule:
        """現在の保存行から元の token を持つ test DTO を作る。"""

        return _claimed(self.occurrences[0], token=TOKEN)

    def add(self, row: object) -> None:
        """Schedule/occurrence の追加以外を黙って成功にしない。"""

        if isinstance(row, TaskScheduleOccurrence):
            self.occurrences.append(row)
        elif isinstance(row, TaskSchedule):
            self.schedule = row
            self.schedules.append(row)
        else:
            raise AssertionError(f"Unexpected add type: {type(row).__name__}")

    async def scalar(self, statement: Any) -> Any:
        """fake が保証する SQL 部分だけを評価し、未対応を失敗させる。"""

        self.statements.append(statement)
        sql = str(statement)
        params = statement.compile().params
        if sql.startswith("SELECT count(*)"):
            assert len(statement.get_final_froms()) == 1
            assert str(statement.get_final_froms()[0]) == "task_schedule_occurrences"
            assert set(params) == {"schedule_id_1", "status_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                and_(
                    TaskScheduleOccurrence.schedule_id == params["schedule_id_1"],
                    TaskScheduleOccurrence.status == "PENDING",
                )
            )
            return sum(
                row.schedule_id == params["schedule_id_1"] and row.status == "PENDING"
                for row in self.occurrences
            )
        if sql.startswith("SELECT task_schedules."):
            assert statement.column_descriptions[0]["expr"] is TaskSchedule
            assert set(params) == {"id_1", "project_id_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                and_(
                    TaskSchedule.id == params["id_1"],
                    TaskSchedule.project_id == params["project_id_1"],
                )
            )
            rows = [
                row
                for row in self.schedules
                if params["id_1"] == row.id and params["project_id_1"] == row.project_id
            ]
            if statement._for_update_arg is not None and statement._for_update_arg.skip_locked:
                rows = [row for row in rows if row.id not in self.locked_schedule_ids]
            return rows[0] if rows else None
        if sql.startswith("SELECT task_schedule_occurrences."):
            return next(
                (
                    row
                    for row in self.occurrences
                    if row.id == params.get("id_1", row.id)
                    and row.schedule_id == params.get("schedule_id_1", row.schedule_id)
                    and row.status == params.get("status_1", row.status)
                ),
                None,
            )
        if sql.startswith("SELECT runs.id") and "JOIN task_schedule_occurrences" in sql:
            associations = {row.run_id for row in self.occurrences if row.id != params["id_1"]}
            return next(
                (
                    row.id
                    for row in self.runs
                    if row.id in associations and row.status not in params["status_1"]
                ),
                None,
            )
        if sql.startswith("SELECT runs."):
            return next(
                (
                    row
                    for row in self.runs
                    if row.project_id == params["project_id_1"]
                    and row.task_id == params["task_id_1"]
                    and row.idempotency_key == params["idempotency_key_1"]
                ),
                None,
            )
        raise AssertionError(f"Unexpected query: {sql}")

    async def scalars(self, statement: Any) -> Any:
        """到期・回復の候補を条件付きで返し、lock の実並行性は模擬しない。"""

        self.statements.append(statement)
        sql = str(statement)
        params = statement.compile().params
        if sql.startswith("SELECT runs."):
            rows = [
                row
                for row in self.runs
                if row.project_id == params["project_id_1"]
                and row.task_id == params["task_id_1"]
                and row.idempotency_key == params["idempotency_key_1"]
            ]
            assert len(rows) <= 1
            result = MagicMock()
            result.one_or_none.return_value = rows[0] if rows else None
            return result
        if sql.startswith("SELECT task_schedule_occurrences."):
            return [
                row
                for row in self.occurrences
                if row.status == "PENDING"
                and row.lease_expires_at <= params["lease_expires_at_1"]
                and row.attempt_count < params["attempt_count_1"]
            ][: params["param_1"]]
        if sql.startswith("SELECT task_schedules."):
            if "JOIN task_schedule_occurrences" in sql:
                eligible = {
                    row.schedule_id
                    for row in self.occurrences
                    if row.status == "PENDING"
                    and row.lease_expires_at <= params["lease_expires_at_1"]
                    and row.attempt_count < params["attempt_count_1"]
                }
                candidate_schedules = sorted(
                    (
                        row
                        for row in self.schedules
                        if row.id in eligible
                        and row.occurrence_protocol == params["occurrence_protocol_1"]
                    ),
                    key=lambda row: row.id,
                )
                # lock の待機を再現せず、選択前の除外という意味だけを検証する。
                if statement._for_update_arg is not None and statement._for_update_arg.skip_locked:
                    candidate_schedules = [
                        row for row in candidate_schedules if row.id not in self.locked_schedule_ids
                    ]
                return candidate_schedules[: params["param_1"]]
            if "NOT (EXISTS" in sql and any(row.status == "PENDING" for row in self.occurrences):
                return []
            return [self.schedule]
        raise AssertionError(f"Unexpected query: {sql}")

    @asynccontextmanager
    async def transaction(self) -> Any:
        """例外時に行の mutation と追加を戻す。PostgreSQL の rollback 証明ではない。"""

        original_schedule = self.schedule
        original_schedules = list(self.schedules)
        original_runs = list(self.runs)
        rows = [*self.schedules, *self.occurrences, *self.runs]
        before = [
            (
                row,
                deepcopy(
                    {key: value for key, value in vars(row).items() if key != "_sa_instance_state"}
                ),
            )
            for row in rows
        ]
        original_occurrences = list(self.occurrences)
        try:
            yield self.repo
        except BaseException:
            for row, values in before:
                for key in tuple(vars(row)):
                    if key != "_sa_instance_state" and key not in values:
                        delattr(row, key)
                for key, value in values.items():
                    setattr(row, key, value)
            self.occurrences[:] = original_occurrences
            self.schedule = original_schedule
            self.schedules[:] = original_schedules
            self.runs[:] = original_runs
            raise
