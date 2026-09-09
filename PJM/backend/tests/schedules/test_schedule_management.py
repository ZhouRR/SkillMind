"""管理一覧の実 SELECT と原版 CAS を、外部 DB を持たない局部環境で検証する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.schema import CreateTable, Table

from projectmind.core.hashing import canonical_json
from projectmind.db.models import TaskSchedule
from projectmind.schedules.domain import ScheduleConflictError, ScheduleInvalidError, ScheduleStatus
from projectmind.schedules.repository import ScheduleRepository
from projectmind.schedules.service import ScheduleService
from tests.schedules.authorization_harness import ScheduleAuthorizationDatabase
from tests.schedules.fakes import NOW, schedule_row


def _sqlite_value(value: Any) -> Any:
    """UUID/時刻/JSON の保存表記だけを SQLite の合成表へ合わせる。"""

    if isinstance(value, UUID):
        return value.hex
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return canonical_json(value)
    return value


class ManagementQueries:
    """repository の SELECT を :memory: だけで実行し、PG lock/locale は模擬しない。"""

    def __init__(self, rows: list[TaskSchedule]) -> None:
        """合成行の SQL 射影を持ち、元の ORM 行を read model 変換へ返す。"""

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        dialect_factory: Callable[..., Dialect] = SQLiteDialect
        self.dialect = dialect_factory(paramstyle="named")
        table = TaskSchedule.__table__
        assert isinstance(table, Table)
        self.connection.execute(str(CreateTable(table).compile(dialect=self.dialect)))
        self.rows = {row.id: row for row in rows}
        columns = [column.name for column in TaskSchedule.__table__.columns]
        placeholders = ",".join("?" for _ in columns)
        insert_sql = f"INSERT INTO task_schedules ({','.join(columns)}) VALUES ({placeholders})"
        for row in rows:
            self.connection.execute(
                insert_sql, [_sqlite_value(getattr(row, name)) for name in columns]
            )
        self.statements: list[Any] = []
        self.session = MagicMock()
        self.session.scalar = AsyncMock(side_effect=self.scalar)
        self.session.scalars = AsyncMock(side_effect=self.scalars)
        self.repository = ScheduleRepository(self.session)

    def execute(self, statement: Any) -> sqlite3.Cursor:
        """同じ SQLAlchemy 条件を count と一覧の双方で実際に評価する。"""

        self.statements.append(statement)
        compiled = statement.compile(dialect=self.dialect)
        return self.connection.execute(
            str(compiled), {key: _sqlite_value(value) for key, value in compiled.params.items()}
        )

    async def scalar(self, statement: Any) -> int:
        """件数だけを返し、ページ長を total として補造しない。"""

        return int(self.execute(statement).fetchone()[0])

    async def scalars(self, statement: Any) -> list[TaskSchedule]:
        """フィルター/順序/offset を実行した id から元の合成 ORM 行を返す。"""

        return [self.rows[UUID(row["id"])] for row in self.execute(statement).fetchall()]


@contextmanager
def management_rows() -> Iterator[tuple[ManagementQueries, UUID]]:
    """同時刻、他 Project、帰档、literal wildcard を含む明示データを用意する。"""

    project_id = uuid4()
    definitions = [
        (1, "Nightly ANALYSIS", "analyze", "ACTIVE", NOW - timedelta(days=1)),
        (2, "Budget 100% Ready", "task_check", "PAUSED", NOW),
        (3, "Budget 1000 ready", "checklist", "ARCHIVED", NOW),
        (4, "slash /_%", "Review", "ACTIVE", NOW + timedelta(minutes=1)),
        (5, "other project Nightly", "nightly", "ACTIVE", NOW + timedelta(days=1)),
        (6, "Task collection", "nightly_analysis", "ERROR", NOW - timedelta(days=2)),
    ]
    rows: list[TaskSchedule] = []
    for identity, name, task_key, status, created_at in definitions:
        row = schedule_row()
        row.id = UUID(int=identity)
        row.project_id = uuid4() if identity == 5 else project_id
        row.name, row.task_key, row.status, row.created_at = name, task_key, status, created_at
        rows.append(row)
    queries = ManagementQueries(rows)
    try:
        yield queries, project_id
    finally:
        queries.connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "identities"),
    [
        ("  NIGHTLY  ", [1, 6]),
        ("budget", [3, 2]),
        ("%", [4, 2]),
        ("_", [4, 2, 6]),
        ("/_%", [4]),
        ("missing", []),
        (None, [4, 3, 2, 1, 6]),
        (" \t ", [4, 3, 2, 1, 6]),
    ],
)
async def test_management_search_is_literal_case_insensitive_and_project_scoped(
    query: str | None,
    identities: list[int],
) -> None:
    """名称か task_key の部分一致を同じ条件で count と結果に反映する。"""

    with management_rows() as (queries, project_id):
        page = await queries.repository.list_for_project(
            project_id=project_id, limit=20, offset=0, q=query
        )
    assert [item.schedule_id.int for item in page.items] == identities
    assert page.total == len(identities)
    assert page.limit == 20 and page.offset == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(ScheduleStatus))
async def test_management_status_applies_to_count_and_items(status: ScheduleStatus) -> None:
    """単一状態は name 検索と AND になり、帰档も明示取得できる。"""

    expected = {ScheduleStatus.PAUSED: [2], ScheduleStatus.ARCHIVED: [3]}.get(status, [])
    with management_rows() as (queries, project_id):
        page = await queries.repository.list_for_project(
            project_id=project_id, limit=20, offset=0, q="budget", status=status
        )
    assert [item.schedule_id.int for item in page.items] == expected
    assert page.total == len(expected)


@pytest.mark.asyncio
async def test_management_pages_keep_filtered_total_and_do_not_move_with_tick() -> None:
    """同時刻は id 降順で決まり、next_run_at の更新が前後ページを並べ替えない。"""

    with management_rows() as (queries, project_id):
        first = await queries.repository.list_for_project(project_id=project_id, limit=2, offset=0)
        second = await queries.repository.list_for_project(project_id=project_id, limit=2, offset=2)
        empty = await queries.repository.list_for_project(project_id=project_id, limit=2, offset=99)
        queries.connection.execute("UPDATE task_schedules SET next_run_at = NULL")
        first_again = await queries.repository.list_for_project(
            project_id=project_id, limit=2, offset=0
        )
    assert [item.schedule_id.int for item in first.items] == [4, 3]
    assert [item.schedule_id.int for item in second.items] == [2, 1]
    assert first.total == second.total == empty.total == 5
    assert empty.items == () and empty.offset == 99
    assert [item.schedule_id for item in first_again.items] == [
        item.schedule_id for item in first.items
    ]
    assert "ORDER BY task_schedules.created_at DESC, task_schedules.id DESC" in str(
        queries.statements[-1]
    )


@pytest.mark.asyncio
async def test_management_service_preserves_filters_and_server_total() -> None:
    """公開 service が q/status/page を落とさず、結果長へ total を置換しない。"""

    with management_rows() as (queries, project_id):
        queries.session.__aenter__.return_value = queries.session
        service = ScheduleService(
            MagicMock(return_value=queries.session),
            skill_service=MagicMock(),
            run_service=MagicMock(),
        )
        page = await service.list_schedules(
            project_id=project_id, q="NIGHTLY", status=ScheduleStatus.ERROR, limit=1, offset=0
        )
    assert page.total == 1 and page.limit == 1 and page.offset == 0
    assert [item.schedule_id.int for item in page.items] == [6]


def management_service(
    db: ScheduleAuthorizationDatabase, *, skills: MagicMock | None = None
) -> ScheduleService:
    """実認証付き session を使い、Skill の未呼出だけ必要なら個別に観測する。"""

    db.session.__aenter__.return_value = db.session
    sessions = MagicMock(return_value=db.session)
    return ScheduleService(
        sessions,
        skill_service=skills if skills is not None else db.skills,
        run_service=db.runs_service,
    )


@pytest.mark.asyncio
async def test_status_service_rejects_original_stale_version_before_current_transition() -> None:
    """旧画面からの pause が現在 ARCHIVED でも、原版衝突を先に報告する。"""

    db = ScheduleAuthorizationDatabase()
    db.schedule.row_version = 2
    db.schedule.status = "ARCHIVED"
    with pytest.raises(ScheduleConflictError):
        await management_service(db).change_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            access=db.access,
            target=ScheduleStatus.PAUSED,
            expected_row_version=1,
        )
    assert db.schedule.row_version == 2
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_service_passes_original_version_to_locked_cas() -> None:
    """二度目の同じ行確認にも原版を渡す。既に持鎖中なので実 DB 競争とは呼ばない。"""

    db = ScheduleAuthorizationDatabase()
    original_scalar = db.scalar
    schedule_locks = 0

    async def race(statement: Any) -> Any:
        """同 transaction の不整合な行変更を注入し、repo の防御的 CAS を検証する。"""

        nonlocal schedule_locks
        if (
            statement._for_update_arg is not None
            and statement.column_descriptions[0]["entity"] is TaskSchedule
        ):
            schedule_locks += 1
        if schedule_locks == 2:
            db.schedule.row_version = 2
            db.schedule.status = "ARCHIVED"
        return await original_scalar(statement)

    db.session.scalar.side_effect = race
    with pytest.raises(ScheduleConflictError):
        await management_service(db).change_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            access=db.access,
            target=ScheduleStatus.PAUSED,
            expected_row_version=1,
        )
    assert schedule_locks == 2
    assert db.schedule.status == "ACTIVE"
    assert db.schedule.row_version == 1
    assert db.rollbacks == 1
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_service_applies_current_original_version_once() -> None:
    """現版を明示した操作だけが一度遷移し、同じ旧版の再送を新操作にしない。"""

    db = ScheduleAuthorizationDatabase()
    service = management_service(db)
    result = await service.change_status(
        project_id=db.schedule.project_id,
        schedule_id=db.schedule.id,
        access=db.access,
        target=ScheduleStatus.PAUSED,
        expected_row_version=1,
    )
    assert result.status is ScheduleStatus.PAUSED
    assert result.row_version == 2
    with pytest.raises(ScheduleConflictError):
        await service.change_status(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            access=db.access,
            target=ScheduleStatus.ARCHIVED,
            expected_row_version=1,
        )
    assert db.schedule.status == "PAUSED"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ACTIVE", "ARCHIVED", "COMPLETED"])
async def test_edit_original_conflict_precedes_terminal_and_new_input_resolution(
    status: str,
) -> None:
    """旧草稿の入力を今日の task で検証せず、終態の場合も最初に 409 用衝突を返す。"""

    db = ScheduleAuthorizationDatabase(claimed=True)
    definition = db.claim.definition
    db.schedule.status = status
    skills = MagicMock()
    service = management_service(db, skills=skills)
    with pytest.raises(ScheduleConflictError):
        await service.update_schedule(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            access=db.access,
            name="Changed",
            definition=definition,
            input_json={"invalid_new_input": True},
            sources={},
            expected_row_version=1,
        )
    skills.resolve_task_run.assert_not_called()
    db.session.flush.assert_not_awaited()
    assert db.schedule.row_version == 2
    assert db.schedule.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ARCHIVED", "COMPLETED"])
async def test_edit_same_version_terminal_still_rejects_without_resolving_input(
    status: str,
) -> None:
    """現版を確認していても終態編集を許さず、既存の 422 境界を保持する。"""

    db = ScheduleAuthorizationDatabase(claimed=True)
    definition = db.claim.definition
    db.schedule.status = status
    skills = MagicMock()
    service = management_service(db, skills=skills)
    with pytest.raises(ScheduleInvalidError, match="no longer editable"):
        await service.update_schedule(
            project_id=db.schedule.project_id,
            schedule_id=db.schedule.id,
            access=db.access,
            name="Changed",
            definition=definition,
            input_json={"new_input": True},
            sources={},
            expected_row_version=2,
        )
    skills.resolve_task_run.assert_not_called()
    db.session.flush.assert_not_awaited()
    assert db.schedule.row_version == 2
