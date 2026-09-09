"""0036 の型・認領制約・監査保持を、metadata と接続しない SQL で検証する。

SQLite は制約と guard の評価だけに使い、PostgreSQL の lock/競争/commit は証明しない。
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import Mock, call
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable
from sqlalchemy.sql.elements import conv

from projectmind.db.base import Base
from projectmind.db.models import TaskSchedule, TaskScheduleOccurrence
from tests.db.test_input_snapshot_migration import _contract

_LOCK = "LOCK TABLE task_schedules, task_schedule_occurrences IN ACCESS EXCLUSIVE MODE"
_TABLE = cast(sa.Table, TaskScheduleOccurrence.__table__)
_PARENTS = {
    "task_schedules": str(UUID(int=1)),
    "projects": str(UUID(int=2)),
    "users": str(UUID(int=3)),
    "skill_versions": str(UUID(int=4)),
    "runs": str(UUID(int=5)),
}
_SECOND_SCHEDULE = str(UUID(int=6))
_TIME = "2026-09-09T01:00:00+00:00"
_NULLABLE = {"run_id", "outcome", "detail", "settled_at"}
_NEW_SCHEDULE_COLUMNS = ("configuration_version", "occurrence_protocol")


def _migration(filename: str) -> ModuleType:
    """env.py や設定を実行せず、指定した既存 revision の関数だけを読み込む。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem + "_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """全 operation を fake に置き換え、実 DB と環境 file を参照させない。"""

    module = _migration("0036_schedule_occurrences.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def _indexes(table: sa.Table) -> set[tuple[str, tuple[str, ...], bool, str | None]]:
    """通常 index と PG partial predicate を同じ比較に含める。"""

    return {
        (
            str(index.name),
            tuple(column.name for column in index.columns),
            index.unique,
            str(index.dialect_options["postgresql"]["where"])
            if index.dialect_options["postgresql"]["where"] is not None
            else None,
        )
        for index in table.indexes
    }


def test_0036_composes_with_original_0025_and_matches_orm_without_rewriting_history(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0025 の旧命名を保存したまま、実追加 operation の最終型・制約・index を比較する。"""

    old = _migration("0025_task_schedules.py")
    old_operations = Mock()
    old_operations.f.side_effect = conv
    monkeypatch.setattr(old, "op", old_operations)
    old.upgrade()
    migration.upgrade()
    assert migration.revision == "0036_schedule_occurrences"
    assert migration.down_revision == "0035_budget_invocations"
    assert [entry[0] for entry in migration.op.method_calls] == [
        "add_column",
        "add_column",
        "create_check_constraint",
        "create_check_constraint",
        "create_table",
        "create_index",
    ]
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("projects", "users", "skill_versions", "runs"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for operations in (old_operations, migration.op):
        for operation in operations.add_column.call_args_list:
            table, column = operation.args
            assert table == "task_schedules" and column.name in _NEW_SCHEDULE_COLUMNS
            assert column.nullable is False and column.default is None
            metadata.tables[table].append_column(column)
        for operation in operations.create_check_constraint.call_args_list:
            name, table, condition = operation.args
            metadata.tables[table].append_constraint(sa.CheckConstraint(condition, name=name))
        for operation in operations.create_table.call_args_list:
            name, *elements = operation.args
            sa.Table(name, metadata, *elements)
        for operation in operations.create_index.call_args_list:
            name, table, columns = operation.args
            sa.Index(
                name, *(metadata.tables[table].c[column] for column in columns), **operation.kwargs
            )
    for actual in (cast(sa.Table, TaskSchedule.__table__), _TABLE):
        migrated = metadata.tables[actual.name]
        assert _contract(migrated) == _contract(actual)
        assert _indexes(migrated) == _indexes(actual)
        expected_names = {str(constraint.name) for constraint in actual.constraints}
        if actual.name == "task_schedules":
            # 0025 は短い名前、旧 ORM は table 名を重ねた名前であり、本変更で旧 DDL を改めない。
            expected_names = {
                name.replace("ck_task_schedules_task_schedules_", "ck_task_schedules_")
                for name in expected_names
            }
        assert {str(constraint.name) for constraint in migrated.constraints} == expected_names
    migration.op.execute.assert_not_called()


def test_occurrence_types_and_restrict_references_keep_the_frozen_contract() -> None:
    """原 identity・期限・snapshot と nullable 結算だけを保存し、Run を連鎖削除しない。"""

    assert set(_TABLE.columns.keys()) == {
        "id",
        "schedule_id",
        "project_id",
        "created_by",
        "skill_version_id",
        "occurrence_at",
        "idempotency_key",
        "configuration_version",
        "snapshot_json",
        "snapshot_checksum",
        "status",
        "worker_id",
        "lease_token_hash",
        "lease_generation",
        "lease_expires_at",
        "attempt_count",
        "run_id",
        "outcome",
        "detail",
        "settled_at",
        "created_at",
        "updated_at",
    }
    assert {column.name for column in _TABLE.columns if column.nullable} == _NULLABLE
    assert {key.target_fullname: key.ondelete for key in _TABLE.foreign_keys} == {
        parent + ".id": "RESTRICT" for parent in _PARENTS
    }
    for name in ("occurrence_at", "lease_expires_at", "settled_at", "created_at", "updated_at"):
        column_type = _TABLE.c[name].type
        assert isinstance(column_type, sa.DateTime) and column_type.timezone is True
    for name, length in (
        ("idempotency_key", 200),
        ("snapshot_checksum", 64),
        ("status", 16),
        ("worker_id", 128),
        ("lease_token_hash", 64),
        ("outcome", 32),
        ("detail", 512),
    ):
        column_type = _TABLE.c[name].type
        assert isinstance(column_type, sa.String) and column_type.length == length
    assert isinstance(_TABLE.c.snapshot_json.type, sa.JSON)
    assert _indexes(_TABLE) == {
        ("uq_task_schedule_occurrence_pending", ("schedule_id",), True, "status = 'PENDING'")
    }
    assert {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in _TABLE.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    } == {
        ("uq_task_schedule_occurrence_identity", ("schedule_id", "occurrence_at")),
        ("uq_task_schedule_occurrence_run", ("run_id",)),
    }


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    """ORM の実制約を SQLite に適用し、合成親 ID だけを持つ局部検証 DB を閉じる。"""

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for table, identifier in _PARENTS.items():
            connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
            connection.execute(f"INSERT INTO {table} VALUES (?)", (identifier,))
        connection.execute("INSERT INTO task_schedules VALUES (?)", (_SECOND_SCHEDULE,))
        connection.execute(str(CreateTable(_TABLE).compile(dialect=sqlite.dialect())))
        # PG 専用 predicate を無視した SQLite 全行 unique にしない。同じ SQL 条件を明示評価する。
        index = next(iter(_TABLE.indexes))
        dialect = MigrationContext.configure(dialect_name="postgresql").dialect
        connection.execute(str(CreateIndex(index).compile(dialect=dialect)))
        yield connection


def _record(**changes: str | int | None) -> dict[str, str | int | None]:
    """値の意味検証と DB 制約検証を混同せず、合法な合成の原予定を用意する。"""

    return {
        "id": str(UUID(int=20)),
        "schedule_id": _PARENTS["task_schedules"],
        "project_id": _PARENTS["projects"],
        "created_by": _PARENTS["users"],
        "skill_version_id": _PARENTS["skill_versions"],
        "occurrence_at": _TIME,
        "idempotency_key": "synthetic-original-key",
        "configuration_version": 1,
        "snapshot_json": "{}",
        "snapshot_checksum": "a" * 64,
        "status": "PENDING",
        "worker_id": "synthetic-worker",
        "lease_token_hash": "b" * 64,
        "lease_generation": 1,
        "lease_expires_at": _TIME,
        "attempt_count": 1,
        "run_id": None,
        "outcome": None,
        "detail": None,
        "settled_at": None,
        "created_at": _TIME,
        "updated_at": _TIME,
        **changes,
    }


def _insert(database: sqlite3.Connection, **changes: str | int | None) -> None:
    """列名は固定 metadata、合成値は bind parameter として渡す。"""

    columns = list(_TABLE.columns.keys())
    database.execute(
        f"INSERT INTO task_schedule_occurrences ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + column for column in columns)})",
        _record(**changes),
    )


@pytest.mark.parametrize("status", ["PENDING", "SETTLED", "UNKNOWN"])
@pytest.mark.parametrize(
    "outcome", [None, "RUN_CREATED", "SKIPPED_OVERLAP", "FAILED_PRECONDITION", "UNKNOWN"]
)
@pytest.mark.parametrize("run_present", [False, True])
@pytest.mark.parametrize("settled_present", [False, True])
def test_settlement_check_rejects_partial_or_contradictory_results(
    database: sqlite3.Connection,
    status: str,
    outcome: str | None,
    run_present: bool,
    settled_present: bool,
) -> None:
    """SQL NULL の三値論理で未完結や run/outcome の矛盾が通過しないことを実評価する。"""

    allowed = (
        status == "PENDING" and outcome is None and not run_present and not settled_present
    ) or (
        status == "SETTLED"
        and settled_present
        and outcome in {"RUN_CREATED", "SKIPPED_OVERLAP", "FAILED_PRECONDITION"}
        and run_present == (outcome == "RUN_CREATED")
    )
    values = {
        "status": status,
        "outcome": outcome,
        "run_id": _PARENTS["runs"] if run_present else None,
        "settled_at": _TIME if settled_present else None,
    }
    if allowed:
        _insert(database, **values)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert(database, **values)
    assert database.execute("SELECT count(*) FROM task_schedule_occurrences").fetchone() == (
        int(allowed),
    )


@pytest.mark.parametrize("column", ["configuration_version", "lease_generation", "attempt_count"])
@pytest.mark.parametrize("value", [-1, 0, 1])
def test_occurrence_versions_and_attempts_must_be_positive(
    database: sqlite3.Connection,
    column: str,
    value: int,
) -> None:
    """世代と試行回数を零へ戻して、旧所有者や未試行へ偽装できない。"""

    if value > 0:
        _insert(database, **{column: value})
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert(database, **{column: value})


@pytest.mark.parametrize(
    "column", [column.name for column in _TABLE.columns if not column.nullable]
)
def test_required_occurrence_columns_reject_sql_null(
    database: sqlite3.Connection,
    column: str,
) -> None:
    """原 identity や claim の必要列を省略した行を残さない。"""

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        _insert(database, **{column: None})


def test_occurrence_identity_and_single_pending_are_independent_constraints(
    database: sqlite3.Connection,
) -> None:
    """同予定の再挿入と別予定の二重認領を拒否し、結算後の次回や別 Schedule は許す。"""

    _insert(database)
    for time in (_TIME, "2026-09-09T02:00:00+00:00"):
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            _insert(database, id=str(UUID(int=21)), occurrence_at=time)
    database.execute(
        "UPDATE task_schedule_occurrences SET status = 'SETTLED', "
        "outcome = 'SKIPPED_OVERLAP', settled_at = ?",
        (_TIME,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(database, id=str(UUID(int=21)))
    _insert(database, id=str(UUID(int=21)), occurrence_at="2026-09-09T02:00:00+00:00")
    _insert(database, id=str(UUID(int=22)), schedule_id=_SECOND_SCHEDULE)
    assert database.execute("SELECT count(*) FROM task_schedule_occurrences").fetchone() == (3,)


def test_a_run_cannot_be_counted_by_two_schedule_occurrences(database: sqlite3.Connection) -> None:
    """Schedule が異なっても同一 Run の第二結算を global unique が拒否する。"""

    result = {
        "status": "SETTLED",
        "outcome": "RUN_CREATED",
        "run_id": _PARENTS["runs"],
        "settled_at": _TIME,
    }
    _insert(database, **result)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(database, id=str(UUID(int=21)), schedule_id=_SECOND_SCHEDULE, **result)


@pytest.mark.parametrize(
    ("column", "parent"),
    [
        ("schedule_id", "task_schedules"),
        ("project_id", "projects"),
        ("created_by", "users"),
        ("skill_version_id", "skill_versions"),
        ("run_id", "runs"),
    ],
)
def test_occurrence_foreign_keys_reject_missing_parent_and_preserve_referenced_history(
    database: sqlite3.Connection,
    column: str,
    parent: str,
) -> None:
    """未解決参照と親削除を拒否し、結算済みも cascade/SET NULL で消さない。"""

    result = {
        "status": "SETTLED",
        "outcome": "RUN_CREATED",
        "run_id": _PARENTS["runs"],
        "settled_at": _TIME,
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert(database, **{**result, column: str(UUID(int=99))})
    _insert(database, **result)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        database.execute(f"DELETE FROM {parent} WHERE id = ?", (_PARENTS[parent],))
    assert database.execute("SELECT count(*) FROM task_schedule_occurrences").fetchone() == (1,)


def test_upgrade_defaults_preserve_legacy_summaries_and_do_not_enable_old_writers(
    migration: ModuleType,
) -> None:
    """新列の実 DEFAULT だけを既存行へ適用し、旧計数や last pointer を補正しない。"""

    migration.upgrade()
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE task_schedules (id TEXT, run_count INTEGER, last_run_id TEXT)"
        )
        database.execute("INSERT INTO task_schedules VALUES ('old', 17, 'old-run')")
        for operation in migration.op.add_column.call_args_list:
            table, column = operation.args
            ddl = str(CreateColumn(column).compile(dialect=sqlite.dialect()))
            database.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        database.execute("INSERT INTO task_schedules (id, run_count) VALUES ('old-writer', 0)")
        assert database.execute("SELECT * FROM task_schedules ORDER BY id").fetchall() == [
            ("old", 17, "old-run", 1, 0),
            ("old-writer", 0, None, 1, 0),
        ]
    for name in _NEW_SCHEDULE_COLUMNS:
        assert TaskSchedule.__table__.c[name].default is None
    migration.op.execute.assert_not_called()


@pytest.mark.parametrize("protocol", [-1, 0, 1, 2])
@pytest.mark.parametrize("version", [0, 1, 2])
def test_schedule_protocol_and_configuration_checks_reject_unknown_values(
    protocol: int,
    version: int,
) -> None:
    """旧 protocol 0 と新 1 だけを許可し、設定版の零を通さない。"""

    checks = [
        str(item.sqltext)
        for item in TaskSchedule.__table__.constraints
        if isinstance(item, sa.CheckConstraint)
        and item.name
        in {
            "ck_task_schedules_configuration_version",
            "ck_task_schedules_occurrence_protocol",
        }
    ]
    assert len(checks) == 2
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE configuration (occurrence_protocol INTEGER NOT NULL, "
            "configuration_version INTEGER NOT NULL, "
            + ", ".join(f"CHECK ({check})" for check in checks)
            + ")"
        )
        if protocol in (0, 1) and version >= 1:
            database.execute("INSERT INTO configuration VALUES (?, ?)", (protocol, version))
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("INSERT INTO configuration VALUES (?, ?)", (protocol, version))


@pytest.mark.parametrize("protocol", [0, 1])
@pytest.mark.parametrize("version", [0, 1, 2])
@pytest.mark.parametrize("occurrence", [None, "PENDING", "SETTLED", "UNKNOWN"])
def test_downgrade_evaluates_actual_guard_after_lock_and_preserves_all_rows(
    migration: ModuleType,
    protocol: int,
    version: int,
    occurrence: str | None,
) -> None:
    """使用済み protocol・設定版・全状態の監査を、DDL 前に実 SQL 条件で保全する。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE task_schedules (occurrence_protocol INTEGER, "
            "configuration_version INTEGER, run_count INTEGER)"
        )
        database.execute("INSERT INTO task_schedules VALUES (?, ?, 17)", (protocol, version))
        database.execute("CREATE TABLE task_schedule_occurrences (status TEXT)")
        rows = [] if occurrence is None else [(occurrence,)]
        database.executemany("INSERT INTO task_schedule_occurrences VALUES (?)", rows)

        def execute(statement: str) -> None:
            """lock は順序だけ検査し、DO の原述語を変更せずローカル SQL で評価する。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Schedule occurrences and enabled protocols must be preserved "
                r"before downgrade'; END IF; END \$\$;",
                statement,
            )
            assert match is not None, "Unexpected SQL or history rewrite"
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None and result[0] in (0, 1)
            if result[0]:
                raise RuntimeError("preserve schedule history")

        migration.op.execute.side_effect = execute
        if occurrence is not None or protocol == 1 or version != 1:
            with pytest.raises(RuntimeError, match="preserve schedule history"):
                migration.downgrade()
            assert [entry[0] for entry in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            migration.op.drop_table.assert_called_once_with("task_schedule_occurrences")
            assert [entry.args for entry in migration.op.drop_column.call_args_list] == [
                ("task_schedules", name) for name in reversed(_NEW_SCHEDULE_COLUMNS)
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM task_schedules").fetchall() == [
            (protocol, version, 17)
        ]
        assert database.execute("SELECT * FROM task_schedule_occurrences").fetchall() == rows


def test_downgrade_lock_failure_stops_before_any_guard_or_drop(migration: ModuleType) -> None:
    """親から子への lock を取れない場合、削除可能との推測で進まない。"""

    migration.op.execute.side_effect = RuntimeError("lock unavailable")
    with pytest.raises(RuntimeError, match="lock unavailable"):
        migration.downgrade()
    assert migration.op.method_calls == [call.execute(_LOCK)]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_postgresql_offline_ddl_preserves_partial_index_names_and_no_history_rewrite(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    """実 PG compiler と Alembic の naming convention を、DB 接続なしで通す。"""

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output, "target_metadata": Base.metadata},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, direction)()
    sql = output.getvalue()
    assert not re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", sql)
    assert "CASCADE" not in sql
    if direction == "upgrade":
        assert "ADD COLUMN configuration_version INTEGER DEFAULT 1 NOT NULL" in sql
        assert "ADD COLUMN occurrence_protocol INTEGER DEFAULT 0 NOT NULL" in sql
        assert "CREATE UNIQUE INDEX uq_task_schedule_occurrence_pending ON " in sql
        assert "task_schedule_occurrences (schedule_id) WHERE status = 'PENDING'" in sql
        assert (
            "CONSTRAINT uq_task_schedule_occurrence_identity UNIQUE (schedule_id, occurrence_at)"
            in sql
        )
        assert "CONSTRAINT uq_task_schedule_occurrence_run UNIQUE (run_id)" in sql
        for constraint in _TABLE.constraints:
            assert f"CONSTRAINT {constraint.name} " in sql
        assert sql.count("ON DELETE RESTRICT") == 5
    else:
        assert sql.index(_LOCK) < sql.index("DO $$") < sql.index("DROP INDEX")
        assert sql.index("DROP INDEX") < sql.index("DROP TABLE") < sql.index("DROP CONSTRAINT")
        assert sql.index("DROP CONSTRAINT") < sql.index("DROP COLUMN")
        for name in _NEW_SCHEDULE_COLUMNS:
            assert f"DROP CONSTRAINT ck_task_schedules_{name};" in sql
