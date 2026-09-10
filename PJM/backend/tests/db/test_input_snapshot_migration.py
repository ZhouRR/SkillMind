"""入力回执の model/DDL と、監査を消さない downgrade の呼出し順を検証する。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext

from projectmind.db.models import RunInputSnapshot


def _migration() -> ModuleType:
    """DB を開かずに 0029 の entry point を取得する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions/0029_run_input_snapshots.py"
    spec = importlib.util.spec_from_file_location("input_snapshot_migration_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contract(table: sa.Table) -> dict[str, Any]:
    """Python default と列の宣言順を除き、PostgreSQL 上の制約と型を比較する。"""

    dialect = MigrationContext.configure(dialect_name="postgresql").dialect
    return {
        "columns": {
            column.name: (
                column.type.compile(dialect=dialect),
                column.nullable,
                column.primary_key,
                _server_default(column),
            )
            for column in table.columns
        },
        "foreign_keys": {
            (key.parent.name, key.target_fullname, key.ondelete) for key in table.foreign_keys
        },
        "unique": {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        },
        "checks": {
            " ".join(str(constraint.sqltext).split())
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        },
    }


def _server_default(column: sa.Column[object]) -> str | None:
    """DDL の明示 default だけを比較し、不明な server 生成値を空値へ隠さない。"""

    value = column.server_default
    if value is None:
        return None
    assert isinstance(value, sa.DefaultClause)
    return str(value.arg)


def test_input_receipt_migration_and_model_have_the_same_database_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 唯一・RESTRICT・完成条件を migration と model の両方で固定する。"""

    migration = _migration()
    create = Mock()
    monkeypatch.setattr(migration.op, "create_table", create)
    migration.upgrade()
    create.assert_called_once()
    name, *elements = create.call_args.args
    assert name == "run_input_snapshots"
    actual = cast(sa.Table, RunInputSnapshot.__table__)
    metadata = sa.MetaData(naming_convention=actual.metadata.naming_convention)
    for parent in ("runs", "projects", "run_attempts"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    migrated = sa.Table(name, metadata, *elements)
    contract = _contract(actual)
    assert _contract(migrated) == contract
    assert contract["unique"] == {("run_id",)}
    assert contract["foreign_keys"] == {
        ("run_id", "runs.id", "RESTRICT"),
        ("project_id", "projects.id", "RESTRICT"),
        ("prepared_by_attempt_id", "run_attempts.id", "RESTRICT"),
    }
    assert contract["checks"] == {
        "status IN ('PREPARING', 'READY')",
        "total_files >= 0 AND total_bytes >= 0",
        "(status = 'PREPARING' AND completed_at IS NULL AND tree_checksum IS NULL) OR "
        "(status = 'READY' AND completed_at IS NOT NULL AND tree_checksum IS NOT NULL)",
    }


@pytest.mark.parametrize("blocked", [False, True])
def test_input_receipt_downgrade_checks_retention_before_dropping(
    monkeypatch: pytest.MonkeyPatch, blocked: bool
) -> None:
    """保存済み回执があれば drop を呼ばず、空表だけが検査後に進む順序を守る。"""

    migration = _migration()
    operations = Mock()
    monkeypatch.setattr(migration.op, "execute", operations.execute)
    monkeypatch.setattr(migration.op, "drop_table", operations.drop_table)
    if blocked:
        operations.execute.side_effect = RuntimeError("retention required")
        with pytest.raises(RuntimeError, match="retention required"):
            migration.downgrade()
        operations.drop_table.assert_not_called()
    else:
        migration.downgrade()
        operations.drop_table.assert_called_once_with("run_input_snapshots")
        assert [call[0] for call in operations.mock_calls] == ["execute", "drop_table"]
    operations.execute.assert_called_once()
    sql = " ".join(str(operations.execute.call_args.args[0]).split())
    assert "IF EXISTS (SELECT 1 FROM run_input_snapshots) THEN RAISE EXCEPTION" in sql
    assert "must be preserved before downgrade" in sql
