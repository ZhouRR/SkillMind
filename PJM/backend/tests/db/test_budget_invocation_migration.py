"""0035 の原実行束縛と監査保持を、metadata・offline DDL・メモリ内 SQL で検査する。

SQLite は NULL 条件の実評価だけに使い、PostgreSQL の lock/DDL transaction は証明しない。
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from contextlib import closing
from io import StringIO
from itertools import product
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import Mock, call

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from projectmind.db.base import Base
from projectmind.db.models import (
    RunBudgetAccount,
    RunBudgetObservation,
    RunBudgetReceipt,
    RunBudgetReservation,
)
from tests.db.test_budget_migration import migration_module as ledger_migration
from tests.db.test_input_snapshot_migration import _contract

_LOCK = "LOCK TABLE run_budget_reservations, run_budget_observations IN ACCESS EXCLUSIVE MODE"
_BINDING_COLUMNS = ("invocation_id", "invocation_json", "invocation_checksum")
_CHECK_NAME = "ck_run_budget_reservations_budget_invocation_binding"
_PRESENCE = list(product((False, True), repeat=3))


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """実 DB と env.py を読み込まず、0035 の全 operation を隔離する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0035_budget_invocations.py"
    spec = importlib.util.spec_from_file_location("budget_invocation_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", Mock())
    return module


def test_upgrade_composes_with_0030_and_matches_current_budget_models(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧三表に 0035 を適用し、0043 の owner 追加だけを除く歴史契約と比較する。"""

    old = ledger_migration()
    old_operations = Mock()
    monkeypatch.setattr(old, "op", old_operations)
    old.upgrade()
    migration.upgrade()
    assert migration.revision == "0035_budget_invocations"
    assert migration.down_revision == "0034_project_row_version"
    operations = migration.op
    assert [operation[0] for operation in operations.method_calls] == [
        "add_column",
        "add_column",
        "add_column",
        "create_unique_constraint",
        "create_unique_constraint",
        "create_check_constraint",
        "create_table",
    ]
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("runs", "run_segments", "run_attempts"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for operation in old_operations.create_table.call_args_list:
        name, *elements = operation.args
        sa.Table(name, metadata, *elements)
    for operation in operations.add_column.call_args_list:
        name, column = operation.args
        assert name == "run_budget_reservations"
        assert column.name in _BINDING_COLUMNS and column.nullable is True
        assert column.default is None and column.server_default is None
        metadata.tables[name].append_column(column)
    for operation in operations.create_unique_constraint.call_args_list:
        name, table, columns = operation.args
        metadata.tables[table].append_constraint(sa.UniqueConstraint(*columns, name=name))
    name, table, condition = operations.create_check_constraint.call_args.args
    metadata.tables[table].append_constraint(sa.CheckConstraint(condition, name=name))
    name, *elements = operations.create_table.call_args.args
    sa.Table(name, metadata, *elements)
    for model in (RunBudgetAccount, RunBudgetReservation, RunBudgetReceipt, RunBudgetObservation):
        actual = model.__table__
        assert isinstance(actual, sa.Table)
        migrated = metadata.tables[actual.name]
        expected = _contract(actual)
        excluded = set()
        if actual.name == "run_budget_reservations":
            del expected["columns"]["invocation_start_owner_hash"]
            owner_checks = {
                check for check in expected["checks"] if "invocation_start_owner_hash" in check
            }
            assert len(owner_checks) == 1
            expected["checks"] -= owner_checks
            excluded = {
                constraint.name
                for constraint in actual.constraints
                if isinstance(constraint, sa.CheckConstraint)
                and "invocation_start_owner_hash" in str(constraint.sqltext)
            }
        assert _contract(migrated) == expected
        assert {constraint.name for constraint in migrated.constraints} == {
            constraint.name for constraint in actual.constraints if constraint.name not in excluded
        }
    for table in (metadata.tables["run_budget_reservations"], RunBudgetReservation.__table__):
        assert isinstance(table, sa.Table)
        column_type = table.c.invocation_json.type
        assert isinstance(column_type, sa.JSON) and column_type.none_as_null is True
        bind = cast(sa.types.TypeEngine[object], column_type).bind_processor(
            MigrationContext.configure(dialect_name="postgresql").dialect
        )
        assert bind is not None and bind(None) is None


def test_raw_observation_has_one_composite_restrict_parent_and_no_settlement_columns() -> None:
    """同じ invocation を別予約へ横付けせず、原始観察を停止や最終消費に見せない。"""

    reservations = RunBudgetReservation.__table__
    assert isinstance(reservations, sa.Table)
    uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in reservations.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert uniques["uq_run_budget_invocation_id"] == ("invocation_id",)
    assert uniques["uq_run_budget_reservation_invocation"] == ("id", "invocation_id")
    observation = RunBudgetObservation.__table__
    assert isinstance(observation, sa.Table)
    assert set(observation.columns.keys()) == {
        "id",
        "reservation_id",
        "invocation_id",
        "observation_key",
        "payload_json",
        "payload_checksum",
        "reconcile_worker_id",
        "created_at",
    }
    assert all(column.nullable is False for column in observation.columns)
    timestamp = observation.c.created_at.type
    assert isinstance(timestamp, sa.DateTime) and timestamp.timezone is True
    for name, length in (
        ("observation_key", 128),
        ("payload_checksum", 64),
        ("reconcile_worker_id", 128),
    ):
        column_type = observation.c[name].type
        assert isinstance(column_type, sa.String) and column_type.length == length
    assert isinstance(observation.c.payload_json.type, sa.JSON)
    assert isinstance(observation.c.reservation_id.type, sa.Uuid)
    assert isinstance(observation.c.invocation_id.type, sa.Uuid)
    assert len(observation.foreign_key_constraints) == 1
    foreign_key = next(iter(observation.foreign_key_constraints))
    assert foreign_key.name == "fk_run_budget_observation_invocation"
    assert foreign_key.ondelete == "RESTRICT"
    assert [(item.parent.name, item.target_fullname) for item in foreign_key.elements] == [
        ("reservation_id", "run_budget_reservations.id"),
        ("invocation_id", "run_budget_reservations.invocation_id"),
    ]
    assert {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in observation.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    } == {("uq_run_budget_observation_key", ("reservation_id", "observation_key"))}


@pytest.mark.parametrize("present", _PRESENCE)
def test_binding_check_accepts_only_all_sql_null_or_complete_binding(
    present: tuple[bool, bool, bool],
) -> None:
    """実際の CheckConstraint を使い、部分束縛と全 SQL NULL の旧行を区別する。"""

    table = RunBudgetReservation.__table__
    assert isinstance(table, sa.Table)
    check = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name == _CHECK_NAME
    )
    values = tuple(
        value if exists else None
        for exists, value in zip(present, ("synthetic-invocation", "{}", "a" * 64), strict=True)
    )
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE binding (invocation_id TEXT, invocation_json TEXT, "
            f"invocation_checksum TEXT, CHECK ({check.sqltext}))"
        )
        if all(present) or not any(present):
            database.execute("INSERT INTO binding VALUES (?, ?, ?)", values)
            assert database.execute("SELECT * FROM binding").fetchone() == values
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("INSERT INTO binding VALUES (?, ?, ?)", values)
            assert database.execute("SELECT * FROM binding").fetchall() == []


@pytest.mark.parametrize("present", _PRESENCE)
@pytest.mark.parametrize("raw", [False, True])
def test_downgrade_evaluates_actual_retention_guard_and_keeps_every_row(
    migration: ModuleType,
    present: tuple[bool, bool, bool],
    raw: bool,
) -> None:
    """三列のどれかだけ残った破損行も拒否し、guard の実 SQL と drop の順序を検査する。"""

    values = tuple(
        value if exists else None
        for exists, value in zip(present, ("synthetic-invocation", "{}", "a" * 64), strict=True)
    )
    with closing(sqlite3.connect(":memory:")) as database:
        # 整合制約を外した fixture でも、一列の痕跡だけで履歴を消せないことを守る。
        database.execute(
            "CREATE TABLE run_budget_reservations (invocation_id TEXT, "
            "invocation_json TEXT, invocation_checksum TEXT)"
        )
        database.execute("CREATE TABLE run_budget_observations (observation_key TEXT)")
        database.execute("INSERT INTO run_budget_reservations VALUES (?, ?, ?)", values)
        raw_rows = [("raw-observation",)] if raw else []
        database.executemany("INSERT INTO run_budget_observations VALUES (?)", raw_rows)

        def execute(statement: str) -> None:
            """lock は記録のみ、元の DO 内条件は無変換でローカル SQL に評価させる。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Budget invocation bindings and raw observations must be preserved "
                r"before downgrade'; END IF; END \$\$;",
                statement,
            )
            assert match is not None, "Unexpected SQL or audit-changing statement"
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None and result[0] in (0, 1)
            if result[0]:
                raise RuntimeError("preserve invocation audit")

        migration.op.execute.side_effect = execute
        if any(present) or raw:
            with pytest.raises(RuntimeError, match="preserve invocation audit"):
                migration.downgrade()
            assert migration.op.method_calls == [
                call.execute(_LOCK),
                call.execute(migration.op.execute.call_args_list[1].args[0]),
            ]
        else:
            migration.downgrade()
            migration.op.drop_table.assert_called_once_with("run_budget_observations")
            assert [item.args for item in migration.op.drop_column.call_args_list] == [
                ("run_budget_reservations", name) for name in reversed(_BINDING_COLUMNS)
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM run_budget_reservations").fetchall() == [values]
        assert database.execute("SELECT * FROM run_budget_observations").fetchall() == raw_rows


def test_downgrade_lock_failure_stops_before_guard_or_ddl(migration: ModuleType) -> None:
    """二表を同じ transaction で止められない場合、存在判定も削除も開始しない。"""

    migration.op.execute.side_effect = RuntimeError("lock unavailable")
    with pytest.raises(RuntimeError, match="lock unavailable"):
        migration.downgrade()
    assert migration.op.method_calls == [call.execute(_LOCK)]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_postgresql_offline_ddl_uses_matching_constraints_and_no_audit_rewrite(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    """実 Alembic/PG compiler を接続なしで使い、命名規約や組合 FK の退行を検出する。"""

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, direction)()
    sql = output.getvalue()
    assert not re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", sql)
    assert "CASCADE" not in sql
    if direction == "upgrade":
        assert "ADD COLUMN invocation_id UUID" in sql
        assert "ADD COLUMN invocation_json JSON" in sql
        assert "ADD COLUMN invocation_checksum VARCHAR(64)" in sql
        assert "CONSTRAINT uq_run_budget_invocation_id UNIQUE (invocation_id)" in sql
        assert "CONSTRAINT uq_run_budget_reservation_invocation UNIQUE (id, invocation_id)" in sql
        assert f"CONSTRAINT {_CHECK_NAME} CHECK" in sql
        assert (
            "CONSTRAINT fk_run_budget_observation_invocation FOREIGN KEY(reservation_id, "
            "invocation_id) REFERENCES run_budget_reservations (id, invocation_id) "
            "ON DELETE RESTRICT"
        ) in sql
    else:
        assert sql.index(_LOCK) < sql.index("DO $$") < sql.index("DROP TABLE")
        assert sql.index("DROP TABLE run_budget_observations") < sql.index("DROP CONSTRAINT")
        assert sql.index("DROP CONSTRAINT") < sql.index("DROP COLUMN")
        for name in (
            _CHECK_NAME,
            "uq_run_budget_reservation_invocation",
            "uq_run_budget_invocation_id",
        ):
            assert f"DROP CONSTRAINT {name};" in sql
