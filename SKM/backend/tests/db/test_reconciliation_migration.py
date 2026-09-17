"""核対台帳の model/DDL、部分一意 index と履歴保護を検証する。"""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.schema import CreateIndex

from skillmind.db.models import EffectReconciliationRequest, McpDesktopLease
from tests.db.test_input_snapshot_migration import _contract


def migration(filename="0047_effect_reconciliation_requests.py"):
    """接続なしで原 migration をロードする。"""
    path = Path(__file__).resolve().parents[2] / "migrations/versions" / filename
    spec = importlib.util.spec_from_file_location("reconciliation_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_model_and_active_index(monkeypatch):
    """NULL receipt と部分一意条件も照合し、旧 Effect に観測を補造しない。"""
    module = migration()
    monkeypatch.setattr(module, "op", Mock())
    module.upgrade()
    actual = EffectReconciliationRequest.__table__
    metadata = sa.MetaData(naming_convention=actual.metadata.naming_convention)
    for parent in (
        "organizations",
        "users",
        "auth_sessions",
        "projects",
        "runs",
        "effect_executions",
    ):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for method, args, kwargs in module.op.method_calls:
        if method == "create_table":
            name, *elements = args
            sa.Table(name, metadata, *elements, **kwargs)
        else:
            assert method == "create_index"
            name, table, columns = args
            sa.Index(name, *(metadata.tables[table].c[column] for column in columns), **kwargs)
    # 原 migration を書換えず、追加 revision の制約差分も適用した head と比較する。
    latest = migration("0052_mcp_desktop_leases.py")
    monkeypatch.setattr(latest, "op", Mock(f=lambda value: value))
    latest.upgrade()
    for method, args, kwargs in latest.op.method_calls:
        if method == "drop_constraint":
            name, table = args
            constraint = next(
                item for item in metadata.tables[table].constraints if item.name == name
            )
            metadata.tables[table].constraints.remove(constraint)
        elif method == "create_check_constraint":
            name, table, condition = args
            metadata.tables[table].append_constraint(
                sa.CheckConstraint(condition, name=sa.schema.conv(name))
            )
        else:
            assert method == "create_table"
            name, *elements = args
            sa.Table(name, metadata, *elements, **kwargs)
    assert _contract(metadata.tables["mcp_desktop_leases"]) == _contract(McpDesktopLease.__table__)
    migrated = metadata.tables[actual.name]
    assert _contract(migrated) == _contract(actual)
    assert {c.name for c in migrated.constraints} == {c.name for c in actual.constraints}
    dialect = MigrationContext.configure(dialect_name="postgresql").dialect
    assert {str(CreateIndex(i).compile(dialect=dialect)) for i in migrated.indexes} == {
        str(CreateIndex(i).compile(dialect=dialect)) for i in actual.indexes
    }
    assert actual.c.receipt_json.type.none_as_null is True
    assert migrated.c.receipt_json.type.none_as_null is True
    assert module.down_revision == "0046_document_library_proposals"
    module.op.execute.assert_not_called()


def test_downgrade_locks_and_refuses_even_terminal_observations(monkeypatch):
    """履歴があれば DROP より先に拒否し、実行中だけの判定へ狭めない。"""
    module = migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    monkeypatch.setattr(module, "op", Operations(context))
    module.downgrade()
    sql = output.getvalue()
    assert sql.index("LOCK TABLE") < sql.index("IF EXISTS") < sql.index("DROP TABLE")
    assert "IF EXISTS (SELECT 1 FROM effect_reconciliation_requests)" in sql
    assert "WHERE" not in sql
