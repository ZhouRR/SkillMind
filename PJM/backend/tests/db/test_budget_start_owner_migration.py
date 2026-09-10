"""0043 の原開始 owner と監査保持を、offline PG DDL と局部 SQL で検証する。

SQLite は NULL/存在条件と固定形式の判定だけに使い、PG の lock・正規表現実装・
DDL transaction や実モデルの停止を証明しない。
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
from unittest.mock import Mock, call

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.schema import CreateColumn

from projectmind.db.base import Base
from projectmind.db.models import (
    RunBudgetAccount,
    RunBudgetObservation,
    RunBudgetReceipt,
    RunBudgetReservation,
)
from tests.db.test_budget_invocation_migration import migration as _invocation_migration
from tests.db.test_budget_migration import migration_module as ledger_migration
from tests.db.test_input_snapshot_migration import _contract

_COLUMN = "invocation_start_owner_hash"
_LOCK = "LOCK TABLE run_budget_reservations IN ACCESS EXCLUSIVE MODE"
_OWNER = "sha256:" + "a" * 64
_BINDING = ("invocation_id", "invocation_json", "invocation_checksum")
invocation_migration = _invocation_migration


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """env.py や実 DB を開かず、新 revision の operation だけを取得する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0043_budget_start_owner.py"
    spec = importlib.util.spec_from_file_location("budget_start_owner_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", Mock())
    return module


def _owner_check(table: sa.Table) -> sa.CheckConstraint:
    """新 owner の条件を実 ORM から一意に取り、テスト用の緩い条件を作らない。"""

    checks = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and _COLUMN in str(constraint.sqltext)
    ]
    assert len(checks) == 1
    return checks[0]


def test_upgrade_preserves_null_owners_without_defaults_or_binding_backfill(
    migration: ModuleType,
) -> None:
    """空/旧完全束縛のいずれにも開始権を補造せず、追加列を SQL NULL のまま残す。"""

    migration.upgrade()
    assert migration.revision == "0043_budget_start_owner"
    assert migration.down_revision == "0042_document_upload_closures"
    assert [item[0] for item in migration.op.method_calls] == [
        "add_column",
        "create_check_constraint",
    ]
    table, column = migration.op.add_column.call_args.args
    assert table == "run_budget_reservations" and column.name == _COLUMN
    assert isinstance(column.type, sa.String) and column.type.length == 71
    assert column.nullable and column.default is None and column.server_default is None
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE run_budget_reservations "
            "(invocation_id TEXT, invocation_json TEXT, invocation_checksum TEXT)"
        )
        before = [(None, None, None), ("old-id", '{"legacy":true}', "b" * 64)]
        database.executemany("INSERT INTO run_budget_reservations VALUES (?, ?, ?)", before)
        database.execute(f"ALTER TABLE {table} ADD COLUMN {CreateColumn(column)}")
        assert database.execute("SELECT * FROM run_budget_reservations").fetchall() == [
            (*row, None) for row in before
        ]
    migration.op.execute.assert_not_called()


def test_all_three_upgrades_match_the_current_four_budget_models(
    migration: ModuleType,
    invocation_migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0030→0035→0043 の実 operation を合成し、旧 DDL を改変せず ORM と比較する。"""

    original = ledger_migration()
    monkeypatch.setattr(original, "op", Mock())
    original.upgrade()
    invocation_migration.upgrade()
    migration.upgrade()
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("runs", "run_segments", "run_attempts"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for revision in (original, invocation_migration, migration):
        for operation in revision.op.method_calls:
            name, args, _kwargs = operation
            if name == "create_table":
                table, *elements = args
                sa.Table(table, metadata, *elements)
            elif name == "add_column":
                table, column = args
                metadata.tables[table].append_column(column)
            elif name == "create_unique_constraint":
                constraint, table, columns = args
                metadata.tables[table].append_constraint(
                    sa.UniqueConstraint(*columns, name=constraint)
                )
            elif name == "create_check_constraint":
                constraint, table, condition = args
                metadata.tables[table].append_constraint(
                    sa.CheckConstraint(condition, name=constraint)
                )
            else:
                assert name == "create_index"
                index, table, columns = args
                sa.Index(index, *(metadata.tables[table].c[column] for column in columns))
    for model in (RunBudgetAccount, RunBudgetReservation, RunBudgetReceipt, RunBudgetObservation):
        actual = model.__table__
        assert isinstance(actual, sa.Table)
        migrated = metadata.tables[actual.name]
        assert _contract(migrated) == _contract(actual)
        assert {item.name for item in migrated.constraints} == {
            item.name for item in actual.constraints
        }
        assert {
            (item.name, tuple(column.name for column in item.columns)) for item in migrated.indexes
        } == {(item.name, tuple(column.name for column in item.columns)) for item in actual.indexes}


@pytest.mark.parametrize("present", list(product((False, True), repeat=3)))
@pytest.mark.parametrize("owner", [None, _OWNER])
def test_owner_requires_complete_binding_but_old_binding_has_no_fabricated_owner(
    migration: ModuleType,
    present: tuple[bool, bool, bool],
    owner: str | None,
) -> None:
    """新旧二つの実 CHECK を一緒に評価し、owner 単体や部分束縛を許さない。"""

    migration.upgrade()
    table = RunBudgetReservation.__table__
    assert isinstance(table, sa.Table)
    name, target, condition = migration.op.create_check_constraint.call_args.args
    assert target == table.name
    check = _owner_check(table)
    assert " ".join(str(check.sqltext).split()) == " ".join(str(condition).split())
    assert check.name == f"ck_run_budget_reservations_{name}"
    original = next(
        item
        for item in table.constraints
        if isinstance(item, sa.CheckConstraint)
        and item.name == "ck_run_budget_reservations_budget_invocation_binding"
    )
    # PG の ~ を SQLite の REGEXP に置換するだけで、NULL/AND/OR 条件は原文を維持する。
    converted = str(condition).replace(" ~ ", " REGEXP ")
    binding = tuple(
        value if exists else None
        for exists, value in zip(
            present,
            ("original-id", "{}", "c" * 64),
            strict=True,
        )
    )
    values = (*binding, owner)
    allowed = all(present) or (not any(present) and owner is None)
    with closing(sqlite3.connect(":memory:")) as database:
        database.create_function(
            "regexp",
            2,
            lambda pattern, value: bool(isinstance(value, str) and re.fullmatch(pattern, value)),
        )
        database.execute(
            "CREATE TABLE binding (invocation_id TEXT, invocation_json TEXT, "
            "invocation_checksum TEXT, "
            f"{_COLUMN} TEXT, CHECK ({original.sqltext}), CHECK ({converted}))"
        )
        if allowed:
            database.execute("INSERT INTO binding VALUES (?, ?, ?, ?)", values)
            assert database.execute("SELECT * FROM binding").fetchall() == [values]
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("INSERT INTO binding VALUES (?, ?, ?, ?)", values)
            assert database.execute("SELECT * FROM binding").fetchall() == []


@pytest.mark.parametrize(
    "owner",
    [
        "",
        "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        " sha256:" + "a" * 64,
    ],
)
def test_actual_owner_check_rejects_noncanonical_hashes(owner: str) -> None:
    """完全束縛があっても、形式の違う owner を正規 identity と扱わない。"""

    table = RunBudgetReservation.__table__
    assert isinstance(table, sa.Table)
    condition = str(_owner_check(table).sqltext).replace(" ~ ", " REGEXP ")
    with closing(sqlite3.connect(":memory:")) as database:
        database.create_function(
            "regexp",
            2,
            lambda pattern, value: bool(isinstance(value, str) and re.fullmatch(pattern, value)),
        )
        database.execute(
            "CREATE TABLE binding (invocation_id TEXT, invocation_json TEXT, "
            "invocation_checksum TEXT, "
            f"{_COLUMN} TEXT, CHECK ({condition}))"
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            database.execute(
                "INSERT INTO binding VALUES (?, ?, ?, ?)", ("id", "{}", "a" * 64, owner)
            )


@pytest.mark.parametrize("owner", [None, _OWNER, "", "malformed"])
@pytest.mark.parametrize("status", ["RESERVED", "START_INTENT", "SETTLED", "RELEASED", "CORRUPT"])
def test_downgrade_retains_every_non_null_owner_even_corrupt_or_settled(
    migration: ModuleType,
    owner: str | None,
    status: str,
) -> None:
    """status や形式で保持条件を弱めず、実 EXISTS と排他→guard→DDL 順序を検査する。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(f"CREATE TABLE run_budget_reservations ({_COLUMN} TEXT, state TEXT)")
        values = (owner, status)
        database.execute("INSERT INTO run_budget_reservations VALUES (?, ?)", values)

        def execute(statement: object) -> None:
            """PG lock は記録し、DO の元存在条件は一切書き換えず局部 SQL へ渡す。"""

            sql = " ".join(str(statement).split())
            if sql == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION '([^']+)'; END IF; END \$\$;",
                sql,
            )
            assert match is not None, "Unexpected SQL or audit rewrite"
            assert "preserv" in match.group(2).lower()
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None and result[0] in (0, 1)
            if result[0]:
                raise RuntimeError("preserve original start owner")

        migration.op.execute.side_effect = execute
        if owner is not None:
            with pytest.raises(RuntimeError, match="preserve original start owner"):
                migration.downgrade()
            assert [item[0] for item in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            assert [item[0] for item in migration.op.method_calls] == [
                "execute",
                "execute",
                "f",
                "drop_constraint",
                "drop_column",
            ]
            migration.op.f.assert_called_once_with(
                "ck_run_budget_reservations_budget_invocation_start_owner"
            )
            migration.op.drop_constraint.assert_called_once_with(
                migration.op.f.return_value, "run_budget_reservations", type_="check"
            )
            migration.op.drop_column.assert_called_once_with("run_budget_reservations", _COLUMN)
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM run_budget_reservations").fetchall() == [values]


def test_downgrade_lock_failure_prevents_guard_and_ddl(migration: ModuleType) -> None:
    """排他 lock が成立しなければ、空判定も監査列の削除も実行しない。"""

    migration.op.execute.side_effect = RuntimeError("lock unavailable")
    with pytest.raises(RuntimeError, match="lock unavailable"):
        migration.downgrade()
    assert migration.op.method_calls == [call.execute(_LOCK)]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_postgresql_offline_ddl_keeps_owner_binding_and_retention_order(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    """PG/Alembic 実 compiler で nullable・正規表現・制約名と保持順を確認する。"""

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
    assert not re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|CASCADE)\b", sql)
    table = RunBudgetReservation.__table__
    assert isinstance(table, sa.Table)
    name = _owner_check(table).name
    if direction == "upgrade":
        assert f"ADD COLUMN {_COLUMN} VARCHAR(71);" in sql
        assert f"CONSTRAINT {name} CHECK" in sql
        assert f"{_COLUMN} ~ '^sha256:[0-9a-f]{{64}}$'" in sql
        for column in _BINDING:
            assert f"{column} IS NOT NULL" in sql
        assert "DEFAULT" not in sql
    else:
        assert sql.index(_LOCK) < sql.index("DO $$") < sql.index("DROP CONSTRAINT")
        assert sql.index("DROP CONSTRAINT") < sql.index("DROP COLUMN")
        assert f"DROP CONSTRAINT {name};" in sql
        assert f"DROP COLUMN {_COLUMN};" in sql
