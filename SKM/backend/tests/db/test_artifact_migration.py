"""Artifact DDL/NULL/保全 guard を接続無しで確認する。PG lock・実迁移は別途検証する。"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from contextlib import closing
from io import StringIO
from itertools import product
from pathlib import Path
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
from sqlalchemy.orm import Session, aliased, make_transient_to_detached
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.elements import conv

from skillmind.db.base import Base
from skillmind.db.models import Evidence
from tests.db.test_input_snapshot_migration import _contract

_COLUMNS = (
    "artifact_ref", "artifact_bytes", "artifact_size", "artifact_mime_type", "artifact_path",
)
_TABLE = cast(sa.Table, Evidence.__table__)
_LOCK = "LOCK TABLE evidence IN ACCESS EXCLUSIVE MODE"
_CHECK_NAME = "ck_evidence_artifact_binding"
_VALID = ("art_original", b"", 0, "text/plain", "output/report.txt")


def _migration(name: str = "0040_evidence_artifacts.py") -> ModuleType:
    """migration 関数だけを取得し、env.py や実 Settings を評価しない。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions" / name
    spec = importlib.util.spec_from_file_location(path.stem + "_artifact_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """DDL と guard の呼出しを記録し、外部 DB 接続を作らない。"""

    module = _migration()
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def _check() -> str:
    """ORM 自身の保存制約を test に再利用し、別の許可規則を作らない。"""

    checks = [str(item.sqltext) for item in _TABLE.constraints
              if isinstance(item, sa.CheckConstraint) and item.name == _CHECK_NAME]
    assert len(checks) == 1
    return checks[0]


def test_0040_nullable_columns_and_check_match_model_without_rewriting_history(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 0004 の形へ追加 DDL を適用し、既存 column/FK と新制約の一致を確認する。"""

    original = _migration("0004_tool_audit_evidence.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(original, "op", operations)
    original.upgrade()
    migration.upgrade()
    assert migration.revision == "0040_evidence_artifacts"
    assert migration.down_revision == "0039_document_blob_cleanups"
    _, *elements = next(
        entry.args for entry in operations.create_table.call_args_list
        if entry.args[0] == "evidence"
    )
    migrated = sa.Table("evidence", sa.MetaData(naming_convention=Base.metadata.naming_convention),
                        *elements)
    for operation in migration.op.add_column.call_args_list:
        table, column = operation.args
        assert table == "evidence" and column.name in _COLUMNS
        assert column.nullable and column.default is None and column.server_default is None
        migrated.append_column(column)
    name, table, condition = migration.op.create_check_constraint.call_args.args
    assert name == "artifact_binding" and table == "evidence"
    migrated.append_constraint(sa.CheckConstraint(condition, name=name))
    expected, actual = _contract(_TABLE), _contract(migrated)
    # 0004 の既存 ev_ref は unique constraint、現 ORM は unique index。同じ既存差分を変更しない。
    assert actual.pop("unique") == {("evidence_ref",)}
    assert expected.pop("unique") == set()
    assert actual == expected
    index = next(item for item in _TABLE.indexes if item.name == "uq_evidence_artifact_ref")
    migration.op.create_index.assert_called_once()
    operation = migration.op.create_index.call_args
    assert operation.args == (index.name, "evidence", ["artifact_ref"])
    assert operation.kwargs["unique"] is True and index.unique
    assert str(operation.kwargs["postgresql_where"]) == str(
        index.dialect_options["postgresql"]["where"]
    )
    migration.op.execute.assert_not_called()


def test_columns_preserve_private_bytea_integer_and_nullable_no_defaults() -> None:
    """private 本文の型と metadata の境界を固定し、空 byte と未導入 NULL を分ける。"""

    assert isinstance(_TABLE.c.artifact_bytes.type, sa.LargeBinary)
    assert isinstance(_TABLE.c.artifact_size.type, sa.Integer)
    assert isinstance(_TABLE.c.artifact_path.type, sa.Text)
    assert isinstance(_TABLE.c.artifact_ref.type, sa.String)
    assert isinstance(_TABLE.c.artifact_mime_type.type, sa.String)
    assert _TABLE.c.artifact_ref.type.length == 64
    assert _TABLE.c.artifact_mime_type.type.length == 255
    for name in _COLUMNS:
        assert _TABLE.c[name].nullable and _TABLE.c[name].server_default is None
    attribute = sa.inspect(Evidence).attrs.artifact_bytes
    assert attribute.deferred and attribute.raiseload
    for entity in (Evidence, aliased(Evidence)):
        assert "artifact_bytes" not in str(sa.select(entity))


def test_private_bytes_raiseload_never_opens_an_implicit_query() -> None:
    """実 ORM の deferred raiseload を無接続 session で通し、SQL 前の拒否を確認する。"""

    # 永続 identity の形だけを構成し、session に flush/commit/DB bind を与えない。
    row = Evidence(id=uuid4())
    make_transient_to_detached(row)
    with Session() as session:
        session.add(row)
        with pytest.raises(sa.exc.InvalidRequestError, match="raiseload=True"):
            _ = row.artifact_bytes


def _check_database(database: sqlite3.Connection) -> None:
    """PG 関数/regex 演算だけを SQLite seam に写し、NULL と CHECK は実 SQL で評価する。"""

    database.create_function("octet_length", 1, lambda value: None if value is None else len(value))
    database.create_function("char_length", 1, lambda value: None if value is None else len(value))
    database.create_function("chr", 1, chr)

    def regexp(pattern: str, value: str | None) -> bool | None:
        """PG の検索演算と ASCII control class だけを Python regex seam に対応させる。"""

        if value is None:
            return None
        return re.search(pattern.replace("[[:cntrl:]]", r"[\x00-\x1f\x7f]"), value) is not None

    database.create_function("regexp", 2, regexp)
    database.execute("PRAGMA case_sensitive_like = ON")
    check = _check().replace(" !~ ", " NOT REGEXP ").replace(" ~ ", " REGEXP ")
    check = check.replace("position(chr(92) in artifact_path)", "instr(artifact_path, chr(92))")
    database.execute(
        "CREATE TABLE evidence (artifact_ref TEXT, artifact_bytes BLOB, artifact_size INTEGER, "
        "artifact_mime_type TEXT, artifact_path TEXT, tool_call_id TEXT, content_hash TEXT, "
        "evidence_ref TEXT, CHECK (" + check + "))"
    )


@pytest.mark.parametrize("present", list(product((False, True), repeat=5)))
def test_check_rejects_every_partial_null_binding_and_accepts_zero_bytes(
    present: tuple[bool, ...],
) -> None:
    """五列の 32 通りを実 CHECK に渡し、SQL UNKNOWN による半保存の許可を防ぐ。"""

    values = tuple(
        value if included else None for value, included in zip(_VALID, present, strict=True)
    )
    values += ("tool", "sha256:" + "a" * 64, "ev_original")
    allowed = all(present) or not any(present)
    with closing(sqlite3.connect(":memory:")) as database:
        _check_database(database)
        if allowed:
            database.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)


@pytest.mark.parametrize("field,value", [
    ("artifact_ref", "wrong"), ("artifact_ref", "art_"), ("artifact_size", -1),
    ("artifact_size", 1), ("artifact_size", 1048577), ("artifact_mime_type", "text/html"),
    ("artifact_path", "workspace/file"), ("artifact_path", "output/"),
    ("artifact_path", "output/a/../b"), ("artifact_path", "output/a/.."),
    ("artifact_path", "output/a/./b"), ("artifact_path", "output//b"),
    ("artifact_path", "output/a\\b"), ("artifact_path", "output/a\n"),
    ("artifact_path", "output/" + "a" * 4090), ("content_hash", "bad"),
    ("evidence_ref", "art_foreign"), ("tool_call_id", None),
])
def test_check_rejects_invalid_complete_binding(field: str, value: Any) -> None:
    """ref/path/size/hash/原 Tool の破損を新 binding に限って拒否する。"""

    row = dict(zip(_COLUMNS, _VALID, strict=True))
    row.update(tool_call_id="tool", content_hash="sha256:" + "a" * 64, evidence_ref="ev_original")
    row[field] = value
    with closing(sqlite3.connect(":memory:")) as database:
        _check_database(database)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            database.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row.values()),
            )


def test_upgrade_does_not_backfill_original_evidence(migration: ModuleType) -> None:
    """元の ID/hash を保持し、追加 nullable DDL は旧書込にも架空 Artifact を発行しない。"""

    migration.upgrade()
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE evidence (evidence_ref TEXT, content_hash TEXT)")
        database.execute("INSERT INTO evidence VALUES ('ev_old', 'original-hash')")
        for operation in migration.op.add_column.call_args_list:
            table, column = operation.args
            ddl = str(CreateColumn(column).compile(dialect=cast(type[Dialect], sqlite.dialect)()))
            database.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        assert database.execute("SELECT * FROM evidence").fetchall() == [
            ("ev_old", "original-hash", None, None, None, None, None),
        ]


@pytest.mark.parametrize("present", list(product((False, True), repeat=5)))
def test_downgrade_guard_preserves_any_value_before_drop(
    migration: ModuleType, present: tuple[bool, ...],
) -> None:
    """空 byte や size=0 と半 binding も保護し、全 NULL の歴史だけ列を戻せる。"""

    values = tuple(
        value if included else None for value, included in zip(_VALID, present, strict=True)
    )
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE evidence (artifact_ref TEXT, artifact_bytes BLOB, artifact_size INTEGER, "
            "artifact_mime_type TEXT, artifact_path TEXT)"
        )
        database.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?)", values)

        def execute(statement: str) -> None:
            """server guard の実 EXISTS を評価し、順序だけの mock 成功で保全を主張しない。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Artifact records must be preserved before downgrade'; END IF; END \$\$;",
                statement,
            )
            assert match is not None
            if database.execute("SELECT " + match.group(1)).fetchone()[0]:
                raise RuntimeError("preserve artifact")

        migration.op.execute.side_effect = execute
        if any(present):
            with pytest.raises(RuntimeError, match="preserve artifact"):
                migration.downgrade()
            assert [item[0] for item in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            assert [item.args for item in migration.op.drop_column.call_args_list] == [
                ("evidence", column) for column in reversed(_COLUMNS)
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM evidence").fetchall() == [values]


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_pg_sql_keeps_constraints_guard_and_no_history_dml(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    """実 PG DDL compiler を接続無しで通し、offline でも保護を省略しない。"""

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={
            "as_sql": True, "output_buffer": output, "target_metadata": Base.metadata,
        },
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, direction)()
    sql = output.getvalue()
    assert not re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", sql)
    assert "CASCADE" not in sql and "DEFAULT" not in sql
    if direction == "upgrade":
        assert "artifact_bytes BYTEA" in sql and "octet_length(artifact_bytes)" in sql
        assert "CREATE UNIQUE INDEX uq_evidence_artifact_ref" in sql
        assert _CHECK_NAME in sql
    else:
        assert sql.index(_LOCK) < sql.index("IF EXISTS") < sql.index("DROP CONSTRAINT")
        assert all(f"{column} IS NOT NULL" in sql for column in _COLUMNS)
