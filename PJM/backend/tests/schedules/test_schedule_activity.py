"""在途公開 read model の単一 SELECT と破損拒否を、実 DB なしで検証する。"""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.exc import MultipleResultsFound, OperationalError
from sqlalchemy.schema import CreateTable, Table

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.db.models import TaskSchedule, TaskScheduleOccurrence
from projectmind.schedules.domain import (
    DEFAULT_SCHEDULE_MAX_ATTEMPTS,
    ScheduleActivityUnavailableError,
    ScheduleNotFoundError,
    ScheduleTracking,
)
from projectmind.schedules.repository import ScheduleRepository
from projectmind.schedules.service import ScheduleService
from tests.schedules.fakes import NOW, occurrence_row, schedule_row


def activity_rows() -> tuple[TaskSchedule, TaskScheduleOccurrence]:
    """元 snapshot codec を共用し、認領時の親 row_version も反映する。"""

    schedule = schedule_row()
    pending = occurrence_row(schedule)
    schedule.row_version = 2
    return schedule, pending


def read_session(
    rows: list[tuple[TaskSchedule, TaskScheduleOccurrence | None, datetime]],
) -> MagicMock:
    """AsyncSession の読取 result だけを模し、書込呼出しを後から検出可能にする。"""

    result = MagicMock()
    if len(rows) > 1:
        result.one_or_none.side_effect = MultipleResultsFound()
    else:
        result.one_or_none.return_value = rows[0] if rows else None
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    session.__aenter__.return_value = session
    return session


def sql_value(value: Any) -> Any:
    """合成 SQLite の bind 表記だけを合わせ、PG の型/隔離を模擬しない。"""

    if isinstance(value, UUID):
        return value.hex
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return canonical_json(value)
    return value


@contextmanager
def sqlite_activity(
    schedules: list[TaskSchedule], occurrences: list[TaskScheduleOccurrence]
) -> Iterator[MagicMock]:
    """実 SELECT の JOIN/WHERE/基数を :memory: で実行し、PG MVCC/時刻証明と区別する。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function("statement_timestamp", 0, lambda: NOW.isoformat())
    dialect_factory: Callable[..., Dialect] = SQLiteDialect
    dialect = dialect_factory(paramstyle="named")
    for model, rows in ((TaskSchedule, schedules), (TaskScheduleOccurrence, occurrences)):
        table = model.__table__
        assert isinstance(table, Table)
        connection.execute(str(CreateTable(table).compile(dialect=dialect)))
        columns = [column.name for column in table.columns]
        placeholders = ",".join("?" for _ in columns)
        insert = f"INSERT INTO {table.name} ({','.join(columns)}) VALUES ({placeholders})"
        for row in rows:
            connection.execute(insert, [sql_value(getattr(row, column)) for column in columns])
    parents = {row.id: row for row in schedules}
    children = {row.id: row for row in occurrences}
    session = read_session([])

    async def execute(statement: Any) -> MagicMock:
        """ORM result の実体だけを合成行へ戻し、検索条件は repository の SQL を使う。"""

        compiled = statement.compile(dialect=dialect)
        selected = connection.execute(
            str(compiled), {key: sql_value(value) for key, value in compiled.params.items()}
        ).fetchall()
        records = [
            (
                parents[UUID(item["id"])],
                children[UUID(item["id_1"])] if item["id_1"] is not None else None,
                datetime.fromisoformat(item["checked_at"]),
            )
            for item in selected
        ]
        return cast(MagicMock, read_session(records).execute.return_value)

    session.execute.side_effect = execute
    try:
        yield session
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_activity_reads_parent_and_pending_in_one_unlocked_statement() -> None:
    """期限切れも読取対象であり、DB 観察時刻と元の各 field をそのまま返す。"""

    schedule, pending = activity_rows()
    pending.attempt_count = DEFAULT_SCHEDULE_MAX_ATTEMPTS
    pending.lease_expires_at = NOW - timedelta(seconds=1)
    before = deepcopy(pending.snapshot_json)
    session = read_session([(schedule, pending, NOW)])
    activity = await ScheduleRepository(session).get_activity(
        project_id=schedule.project_id, schedule_id=schedule.id
    )
    assert activity.tracking is ScheduleTracking.TRACKED
    assert activity.checked_at == NOW
    assert activity.pending is not None
    assert activity.pending.attempt_count == DEFAULT_SCHEDULE_MAX_ATTEMPTS
    assert activity.pending.lease_expires_at < activity.checked_at
    assert pending.snapshot_json == before and pending.status == "PENDING"
    assert schedule.row_version == 2 and schedule.run_count == 0
    assert set(asdict(activity.pending)) == {
        "occurrence_id",
        "occurrence_at",
        "configuration_version",
        "created_at",
        "updated_at",
        "attempt_count",
        "lease_expires_at",
    }
    statement = session.execute.call_args.args[0]
    dialect_factory: Callable[[], Dialect] = PGDialect
    sql = str(statement.compile(dialect=dialect_factory()))
    assert "LEFT OUTER JOIN task_schedule_occurrences" in sql
    assert "statement_timestamp() AS checked_at" in sql
    assert "FOR UPDATE" not in sql and "LIMIT" not in sql
    assert "JOIN runs" not in sql and "FROM runs" not in sql
    assert statement.get_execution_options() == {"populate_existing": True, "autoflush": False}
    assert session.execute.await_count == 1
    session.flush.assert_not_awaited()
    session.add.assert_not_called()
    session.begin.assert_not_called()


@pytest.mark.asyncio
async def test_activity_query_scopes_parent_and_does_not_hide_cross_project_pending() -> None:
    """子 project 条件で隠さず、認可済み親と一致しない台帳は unavailable にする。"""

    schedule, pending = activity_rows()
    other = schedule_row()
    other_pending = occurrence_row(other)
    with sqlite_activity([schedule, other], [pending, other_pending]) as session:
        repo = ScheduleRepository(session)
        activity = await repo.get_activity(project_id=schedule.project_id, schedule_id=schedule.id)
        assert activity.pending is not None and activity.pending.occurrence_id == pending.id
        with pytest.raises(ScheduleNotFoundError):
            await repo.get_activity(project_id=other.project_id, schedule_id=schedule.id)
        with pytest.raises(ScheduleNotFoundError):
            await repo.get_activity(project_id=schedule.project_id, schedule_id=uuid4())
    pending.project_id = other.project_id
    pending.snapshot_json["request"]["project_id"] = str(other.project_id)
    pending.snapshot_checksum = sha256_hex(canonical_json(pending.snapshot_json))
    with (
        sqlite_activity([schedule, other], [pending]) as session,
        pytest.raises(ScheduleActivityUnavailableError),
    ):
        await ScheduleRepository(session).get_activity(
            project_id=schedule.project_id, schedule_id=schedule.id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [0, 1])
async def test_activity_distinguishes_legacy_from_tracked_without_pending(protocol: int) -> None:
    """SETTLED 行や last_* から PENDING を補造せず、旧形式の不明と空を分ける。"""

    schedule, settled = activity_rows()
    schedule.occurrence_protocol = protocol
    settled.status = "SETTLED"
    settled.outcome = "SKIPPED_OVERLAP"
    settled.settled_at = NOW
    with sqlite_activity([schedule], [settled] if protocol else []) as session:
        activity = await ScheduleRepository(session).get_activity(
            project_id=schedule.project_id, schedule_id=schedule.id
        )
    assert activity.pending is None
    assert activity.tracking is (
        ScheduleTracking.TRACKED if protocol else ScheduleTracking.LEGACY_UNAVAILABLE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ACTIVE", "PAUSED", "ARCHIVED", "ERROR", "COMPLETED"])
async def test_activity_preserves_original_pending_after_configuration_or_status_change(
    status: str,
) -> None:
    """親の現在設定を元の認領へ上書きせず、pause/archive でも在途は残す。"""

    schedule, pending = activity_rows()
    schedule.status = status
    schedule.configuration_version = 3
    schedule.row_version = 10
    schedule.name = "Replacement name"
    schedule.input_json = {"replacement": True}
    with sqlite_activity([schedule], [pending]) as session:
        activity = await ScheduleRepository(session).get_activity(
            project_id=schedule.project_id, schedule_id=schedule.id
        )
    assert activity.configuration_version == 3 and activity.row_version == 10
    assert activity.pending is not None and activity.pending.configuration_version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("parent", "occurrence_protocol", 2),
        ("parent", "occurrence_protocol", True),
        ("parent", "occurrence_protocol", 0),
        ("parent", "row_version", 1),
        ("parent", "configuration_version", 0),
        ("parent", "created_by", uuid4()),
        ("parent", "skill_version_id", uuid4()),
        ("parent", "task_key", "replacement"),
        ("pending", "configuration_version", 2),
        ("pending", "snapshot_checksum", "0" * 64),
        ("pending", "snapshot_json", {}),
        ("pending", "attempt_count", True),
        ("pending", "attempt_count", 1.0),
        ("pending", "attempt_count", 0),
        ("pending", "lease_generation", 0),
        ("pending", "lease_expires_at", NOW.replace(tzinfo=None)),
        ("pending", "created_at", NOW.replace(tzinfo=None)),
        ("pending", "updated_at", NOW.replace(tzinfo=None)),
        ("pending", "run_id", uuid4()),
        ("pending", "outcome", "RUN_CREATED"),
        ("pending", "settled_at", NOW),
    ],
)
async def test_activity_rejects_corruption_without_returning_empty(
    target: str,
    field: str,
    value: Any,
) -> None:
    """破損 fixture は SQL 制約を迂回した保存事故の局部注入で、原値を書き換えない。"""

    schedule, pending = activity_rows()
    row = schedule if target == "parent" else pending
    setattr(row, field, value)
    session = read_session([(schedule, pending, NOW)])
    with pytest.raises(ScheduleActivityUnavailableError):
        await ScheduleRepository(session).get_activity(
            project_id=schedule.project_id, schedule_id=schedule.id
        )
    assert getattr(row, field) == value
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_rejects_duplicate_pending_instead_of_selecting_first() -> None:
    """partial UNIQUE 破損の二行を合成し、LIMIT で一件に見せないことを確認する。"""

    schedule, pending = activity_rows()
    second = occurrence_row(schedule)
    second.occurrence_at += timedelta(minutes=1)
    with (
        sqlite_activity([schedule], [pending, second]) as session,
        pytest.raises(ScheduleActivityUnavailableError),
    ):
        await ScheduleRepository(session).get_activity(
            project_id=schedule.project_id, schedule_id=schedule.id
        )


@pytest.mark.asyncio
async def test_activity_service_does_not_authorize_creator_create_run_or_commit() -> None:
    """既存の認可済み読取から先は一回の SELECT だけで、発火 use case を呼ばない。"""

    schedule, pending = activity_rows()
    session = read_session([(schedule, pending, NOW)])
    skills, runs = MagicMock(), MagicMock()
    service = ScheduleService(
        MagicMock(return_value=session), skill_service=skills, run_service=runs
    )
    activity = await service.get_activity(project_id=schedule.project_id, schedule_id=schedule.id)
    assert activity.pending is not None
    assert session.execute.await_count == 1
    assert not skills.mock_calls and not runs.mock_calls
    session.begin.assert_not_called()
    session.commit.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_does_not_mislabel_database_failure_as_empty_or_corrupt() -> None:
    """未知の DB 障害を台帳の既知破損として握り潰さない。"""

    session = read_session([])
    failure = OperationalError("synthetic", {}, RuntimeError("private diagnostic"))
    session.execute.side_effect = failure
    with pytest.raises(OperationalError) as raised:
        await ScheduleRepository(session).get_activity(project_id=uuid4(), schedule_id=uuid4())
    assert raised.value is failure


def test_activity_and_recovery_share_the_default_attempt_limit() -> None:
    """公開観察の上限と Worker default に独立した数値を持たせない。"""

    parameter = inspect.signature(ScheduleRepository.claim_recoverable).parameters["max_attempts"]
    assert parameter.default == DEFAULT_SCHEDULE_MAX_ATTEMPTS
