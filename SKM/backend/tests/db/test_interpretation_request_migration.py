"""原要求/逐次呼出しの DDL と、監査を消さない降級 gate を接続なしで検証する。"""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from skillmind.db.base import Base
from skillmind.db.models import SkillInterpretationCall, SkillInterpretationRequest
from tests.db.test_input_snapshot_migration import _contract


def migration():
    """実 DB や env.py を開かず revision の operation だけをロードする。"""

    path = (
        Path(__file__).resolve().parents[2] / "migrations/versions/0044_interpretation_requests.py"
    )
    spec = importlib.util.spec_from_file_location("interpretation_request_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_both_models_without_backfilling_old_interpretations(monkeypatch) -> None:
    """旧結果に actor/会話/開始を補造せず、空の新台帳だけを追加する。"""

    module = migration()
    monkeypatch.setattr(module, "op", Mock())
    module.upgrade()
    assert module.down_revision == "0043_budget_start_owner"
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in (
        "organizations",
        "users",
        "auth_sessions",
        "skill_sources",
        "skill_interpretations",
    ):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for operation in module.op.method_calls:
        method, args, _kwargs = operation
        if method == "create_table":
            name, *columns = args
            sa.Table(name, metadata, *columns)
        else:
            assert method == "create_index"
            name, table, columns = args
            sa.Index(name, *(metadata.tables[table].c[column] for column in columns))
    for model in (SkillInterpretationRequest, SkillInterpretationCall):
        actual = model.__table__
        assert isinstance(actual, sa.Table)
        migrated = metadata.tables[actual.name]
        assert _contract(migrated) == _contract(actual)
        assert {item.name for item in migrated.constraints} == {
            item.name for item in actual.constraints
        }
        assert {item.name for item in migrated.indexes} == {item.name for item in actual.indexes}
    module.op.execute.assert_not_called()


def test_offline_downgrade_locks_both_tables_and_refuses_any_request(monkeypatch) -> None:
    """終態も含め一行でも残る場合、DROP より先に拒否する SQL を生成する。"""

    module = migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    monkeypatch.setattr(module, "op", Operations(context))
    module.downgrade()
    sql = output.getvalue()
    lock = sql.index(
        "LOCK TABLE skill_interpretation_requests, skill_interpretation_calls "
        "IN ACCESS EXCLUSIVE MODE"
    )
    guard = sql.index("IF EXISTS (SELECT 1 FROM skill_interpretation_requests)")
    calls = sql.index("DROP TABLE skill_interpretation_calls")
    requests = sql.index("DROP TABLE skill_interpretation_requests")
    assert lock < guard < calls < requests
    assert "WHERE" not in sql
