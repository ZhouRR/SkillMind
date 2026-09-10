"""0041 の保存制約・旧行不変・保全 guard を離線 SQL で検証する。実 PG lock 検証ではない。"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from io import StringIO
from types import ModuleType
from typing import Any, cast
from unittest.mock import Mock, call
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.elements import conv

from skillmind.db.base import Base
from skillmind.db.models import Evaluation
from tests.db.test_artifact_migration import _migration
from tests.db.test_input_snapshot_migration import _contract

_TABLE = cast(sa.Table, Evaluation.__table__)
_LOCK = "LOCK TABLE evaluations IN ACCESS EXCLUSIVE MODE"
_CHECK_NAME = "ck_evaluations_submission_binding"
_COLUMNS = ("submission_key", "request_hash")


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """env.py を読まず migration 関数と DDL 記録だけをロードする。"""

    module = _migration("0041_evaluation_submissions.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def _check() -> str:
    """実 ORM の check を別定義の許可ロジックへ置き換えない。"""

    checks = [
        str(item.sqltext)
        for item in _TABLE.constraints
        if isinstance(item, sa.CheckConstraint) and item.name == _CHECK_NAME
    ]
    assert len(checks) == 1
    return checks[0]


def test_0041_migration_and_model_match_without_changing_legacy_fields(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 0007 に追加 DDL を適用し、FK/型/nullable/unique/check の一致を確認する。"""

    original = _migration("0007_evaluations.py")
    old_ops = Mock()
    old_ops.f.side_effect = conv
    monkeypatch.setattr(original, "op", old_ops)
    original.upgrade()
    migration.upgrade()
    assert migration.revision == "0041_evaluation_submissions"
    assert migration.down_revision == "0040_evidence_artifacts"
    name, *elements = old_ops.create_table.call_args.args
    assert name == "evaluations"
    table = sa.Table(
        name, sa.MetaData(naming_convention=Base.metadata.naming_convention), *elements
    )
    for operation in migration.op.add_column.call_args_list:
        table_name, column = operation.args
        assert table_name == name and column.name in _COLUMNS
        assert column.nullable and column.default is None and column.server_default is None
        table.append_column(column)
    check_name, table_name, condition = migration.op.create_check_constraint.call_args.args
    assert table_name == name and check_name == "submission_binding"
    table.append_constraint(sa.CheckConstraint(condition, name=check_name))
    unique_name, table_name, columns = migration.op.create_unique_constraint.call_args.args
    assert table_name == name and unique_name == "uq_evaluations_submission"
    table.append_constraint(sa.UniqueConstraint(*columns, name=unique_name))
    assert _contract(table) == _contract(_TABLE)
    index = next(item for item in _TABLE.indexes if item.name == "ix_evaluations_result_created_id")
    assert migration.op.create_index.call_args.args == (
        index.name,
        name,
        [column.name for column in index.columns],
    )
    assert not index.unique
    assert isinstance(_TABLE.c.submission_key.type, sa.Uuid)
    assert isinstance(_TABLE.c.request_hash.type, sa.String)
    assert _TABLE.c.request_hash.type.length == 71
    migration.op.execute.assert_not_called()


@pytest.mark.parametrize(
    ("key", "checksum", "accepted"),
    [
        (None, None, True),
        (str(uuid4()), "sha256:" + "a" * 64, True),
        (None, "sha256:" + "a" * 64, False),
        (str(uuid4()), None, False),
        ("00000000-0000-0000-0000-000000000000", "sha256:" + "a" * 64, False),
        (str(uuid4()), "sha256:" + "A" * 64, False),
        (str(uuid4()), "a" * 64, False),
        (str(uuid4()), "", False),
        (str(uuid4()), "sha256:" + "a" * 63, False),
    ],
)
def test_pair_nil_and_hash_shape_use_actual_check(
    key: str | None,
    checksum: str | None,
    accepted: bool,
) -> None:
    """PG regex 演算だけを seam で置き換え、NULL と CHECK は実 SQL で判定する。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.create_function(
            "regexp",
            2,
            lambda pattern, value: None if value is None else re.search(pattern, value) is not None,
        )
        database.execute(
            "CREATE TABLE evaluations (submission_key TEXT, request_hash TEXT, CHECK ("
            + _check().replace(" ~ ", " REGEXP ")
            + "))"
        )
        if accepted:
            database.execute("INSERT INTO evaluations VALUES (?, ?)", (key, checksum))
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("INSERT INTO evaluations VALUES (?, ?)", (key, checksum))


def test_unique_scopes_result_actor_and_key_but_allows_legacy_nulls() -> None:
    """実 UNIQUE の三元 scope と SQL NULL の旧追加性を確認する。"""

    unique = next(
        item
        for item in _TABLE.constraints
        if isinstance(item, sa.UniqueConstraint) and item.name == "uq_evaluations_submission"
    )
    columns = ", ".join(column.name for column in unique.columns)
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE evaluations (result_id TEXT, user_id TEXT, submission_key TEXT, "
            f"UNIQUE ({columns}))"
        )
        for row in [
            ("r", "u", None),
            ("r", "u", None),
            ("r", "u", "key"),
            ("r2", "u", "key"),
            ("r", "u2", "key"),
            ("r", "u", "key2"),
        ]:
            database.execute("INSERT INTO evaluations VALUES (?, ?, ?)", row)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            database.execute("INSERT INTO evaluations VALUES ('r', 'u', 'key')")


def test_nullable_addition_keeps_old_row_and_does_not_issue_a_request_key(
    migration: ModuleType,
) -> None:
    """旧原値は維持し、schema 更新は架空の原要求保証を発行しない。"""

    migration.upgrade()
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE evaluations (comment TEXT, revision_json TEXT)")
        database.execute("INSERT INTO evaluations VALUES ('original', '[]')")
        for operation in migration.op.add_column.call_args_list:
            name, column = operation.args
            ddl = str(CreateColumn(column).compile(dialect=cast(type[Dialect], sqlite.dialect)()))
            database.execute(f"ALTER TABLE {name} ADD COLUMN {ddl}")
        assert database.execute("SELECT * FROM evaluations").fetchall() == [
            ("original", "[]", None, None),
        ]


@pytest.mark.parametrize("values", [(None, None), ("key", None), (None, ""), ("key", "hash")])
def test_downgrade_locks_then_protects_any_request_value(
    migration: ModuleType,
    values: tuple[Any, Any],
) -> None:
    """半 binding や空文字も、列を落とす前に実 EXISTS 条件で保護する。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE evaluations (submission_key TEXT, request_hash TEXT)")
        database.execute("INSERT INTO evaluations VALUES (?, ?)", values)

        def execute(statement: str) -> None:
            """PG の DO 包絡だけを剥がし、guard の条件自体は SQL として実行する。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Evaluation submissions must be preserved before downgrade'; END IF; END \$\$;",
                statement,
            )
            assert match is not None
            if database.execute("SELECT " + match.group(1)).fetchone()[0]:
                raise RuntimeError("preserve original request")

        migration.op.execute.side_effect = execute
        if any(value is not None for value in values):
            with pytest.raises(RuntimeError, match="preserve original request"):
                migration.downgrade()
            assert [item[0] for item in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            assert [item.args for item in migration.op.drop_column.call_args_list] == [
                ("evaluations", column) for column in reversed(_COLUMNS)
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM evaluations").fetchall() == [values]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_pg_ddl_has_guard_constraints_and_no_history_dml(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    """実 PostgreSQL compiler で DDL/guard を出し、外部接続無しで順序と非破壊性を確認する。"""

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
    assert "CASCADE" not in sql and "DEFAULT" not in sql
    if direction == "upgrade":
        assert "submission_key UUID" in sql and "request_hash VARCHAR(71)" in sql
        assert "UNIQUE (result_id, user_id, submission_key)" in sql
        assert "CREATE INDEX ix_evaluations_result_created_id" in sql
        assert _CHECK_NAME in sql
    else:
        assert sql.index(_LOCK) < sql.index("IF EXISTS") < sql.index("DROP INDEX")
        assert all(f"{column} IS NOT NULL" in sql for column in _COLUMNS)
