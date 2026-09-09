"""予算の model/DDL 同期と、既存勘定を消さない回退門禁を検証する。"""

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

from projectmind.db.models import RunBudgetAccount, RunBudgetReceipt, RunBudgetReservation
from tests.db.test_input_snapshot_migration import _contract

_LEDGER_TABLES = ("run_budget_accounts", "run_budget_reservations", "run_budget_receipts")
_LOCK = (
    "LOCK TABLE run_budget_accounts, run_budget_reservations, run_budget_receipts "
    "IN ACCESS EXCLUSIVE MODE"
)


def migration_module() -> ModuleType:
    """実 DB へ接続せず、凍結した migration 定義を読み取る。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0030_run_budget_ledger.py"
    spec = importlib.util.spec_from_file_location("budget_migration_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_budget_migration_matches_the_three_persistent_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """金額精度、欠測の NULL、元鍵一意、RESTRICT と停止/結算の制約を一致させる。"""

    migration = migration_module()
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert operations.create_table.call_count == 3
    models: dict[str, sa.Table] = {}
    for model in (RunBudgetAccount, RunBudgetReservation, RunBudgetReceipt):
        table = model.__table__
        assert isinstance(table, sa.Table)
        models[table.name] = table
    metadata = sa.MetaData(naming_convention=RunBudgetAccount.metadata.naming_convention)
    for parent in ("runs", "run_segments", "run_attempts"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for invocation in operations.create_table.call_args_list:
        name, *elements = invocation.args
        migrated = sa.Table(name, metadata, *elements)
        expected = _contract(models[name])
        if name == "run_budget_reservations":
            # 0030 を後続版の DDL に書き換えず、0035 が追加する差分だけを明示で除く。
            for column in ("invocation_id", "invocation_json", "invocation_checksum"):
                del expected["columns"][column]
            expected["unique"] -= {("invocation_id",), ("id", "invocation_id")}
            expected["checks"].remove(
                "(invocation_id IS NULL AND invocation_json IS NULL "
                "AND invocation_checksum IS NULL) "
                "OR (invocation_id IS NOT NULL AND invocation_json IS NOT NULL "
                "AND invocation_checksum IS NOT NULL)"
            )
        assert _contract(migrated) == expected
    operations.create_index.assert_called_once_with(
        "ix_run_budget_group", "run_budget_reservations", ["run_id", "group_key"]
    )
    operations.execute.assert_not_called()


@pytest.mark.parametrize("present", list(product((False, True), repeat=3)))
def test_budget_downgrade_keeps_ledger_and_unknown_obligations(
    monkeypatch: pytest.MonkeyPatch, present: tuple[bool, bool, bool]
) -> None:
    """実際の EXISTS 条件を局部 SQL で評価し、lock→判定→drop の順序も守る。"""

    migration, operations = migration_module(), Mock()
    monkeypatch.setattr(migration, "op", operations)
    with closing(sqlite3.connect(":memory:")) as database:
        for table, populated in zip(_LEDGER_TABLES, present, strict=True):
            database.execute(f"CREATE TABLE {table} (audit_key TEXT)")
            if populated:
                database.execute(f"INSERT INTO {table} VALUES ('retained')")

        def execute(statement: object) -> None:
            """PostgreSQL lock 自体は記録し、元 guard の式だけを無変換で実行する。"""

            sql = " ".join(str(statement).split())
            if sql == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Run budget ledger must be preserved before downgrade'; END IF; END \$\$;",
                sql,
            )
            assert match is not None, "Unexpected SQL or audit-changing statement"
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None and result[0] in (0, 1)
            if result[0]:
                raise RuntimeError("preserve ledger")

        operations.execute.side_effect = execute
        if any(present):
            with pytest.raises(RuntimeError, match="preserve ledger"):
                migration.downgrade()
            operations.drop_table.assert_not_called()
            operations.drop_index.assert_not_called()
        else:
            migration.downgrade()
            assert [item.args[0] for item in operations.drop_table.call_args_list] == list(
                reversed(_LEDGER_TABLES)
            )
        assert operations.method_calls[0] == call.execute(_LOCK)
        assert operations.execute.call_count == 2
        assert operations.method_calls[1] == call.execute(
            operations.execute.call_args_list[1].args[0]
        )
        for table, populated in zip(_LEDGER_TABLES, present, strict=True):
            assert database.execute(f"SELECT * FROM {table}").fetchall() == (
                [("retained",)] if populated else []
            )


def test_budget_downgrade_lock_failure_never_checks_or_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三表の排他を同じ transaction に確保できない場合は存在判定にも進まない。"""

    migration, operations = migration_module(), Mock()
    monkeypatch.setattr(migration, "op", operations)
    operations.execute.side_effect = RuntimeError("lock unavailable")
    with pytest.raises(RuntimeError, match="lock unavailable"):
        migration.downgrade()
    assert operations.method_calls == [call.execute(_LOCK)]


def test_budget_downgrade_postgresql_ddl_locks_before_guard_and_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実 Alembic compiler の出力で空判定と DROP の競争窓を再導入させない。"""

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    migration = migration_module()
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.downgrade()
    sql = output.getvalue()
    assert sql.index(_LOCK) < sql.index("DO $$") < sql.index("DROP TABLE")
    assert not re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|CASCADE)\b", sql)
