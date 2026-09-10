"""公開停止の DDL・元意図保護・無損失回退を、実サービスなしで検証する。

SQLite は制約と guard 述語の局部証拠であり、実 PostgreSQL の時刻型・lock とは区別する。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from io import StringIO
from types import ModuleType
from typing import cast
from unittest.mock import Mock, call
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.elements import conv

from projectmind.db.base import Base
from projectmind.db.models import ProjectDocumentUpload, ProjectDocumentUploadClosure
from tests.db.test_document_storage_namespace_migration import _migration
from tests.db.test_document_upload_intent_migration import _indexes, _insert, _row
from tests.db.test_input_snapshot_migration import _contract

_INTENTS = cast(sa.Table, ProjectDocumentUpload.__table__)
_CLOSURES = cast(sa.Table, ProjectDocumentUploadClosure.__table__)
_LOCK = "LOCK TABLE document_upload_intents, document_upload_closures IN ACCESS EXCLUSIVE MODE"
_TIME = "2026-09-10T12:00:00+00:00"
_AFTER = "2026-09-10T12:00:01+00:00"
_BEFORE = "2026-09-10T11:59:59+00:00"
_PREDICATE = "state = 'PENDING' AND publication_closed_at IS NULL"


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """env.py や実設定を読まず、0042 の実 entry point を取り出す。"""

    module = _migration("0042_document_upload_closures.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def test_0042_composes_with_unchanged_0038_and_matches_current_models(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧意図 DDL をそのまま用い、追加列/制約/index と新監査表を現在 ORM に照合する。"""

    original = _migration("0038_document_upload_intents.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(original, "op", operations)
    original.upgrade()
    migration.upgrade()
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("organizations", "projects", "users"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    name, *elements = original.op.create_table.call_args.args
    intents = sa.Table(name, metadata, *elements)
    for invocation in original.op.create_index.call_args_list:
        index_name, _, columns = invocation.args
        if index_name == "uq_document_upload_intent_pending_path":
            assert str(invocation.kwargs["postgresql_where"]) == "state = 'PENDING'"
            continue
        sa.Index(index_name, *(intents.c[column] for column in columns), **invocation.kwargs)
    table, column = migration.op.add_column.call_args.args
    assert table == intents.name and column.name == "publication_closed_at"
    assert column.nullable and column.default is None and column.server_default is None
    intents.append_column(column)
    check_name, table, condition = migration.op.create_check_constraint.call_args.args
    assert table == intents.name
    intents.append_constraint(sa.CheckConstraint(condition, name=check_name))
    name, *elements = migration.op.create_table.call_args.args
    closures = sa.Table(name, metadata, *elements)
    for invocation in migration.op.create_index.call_args_list:
        index_name, table, columns = invocation.args
        target = metadata.tables[table]
        sa.Index(index_name, *(target.c[column] for column in columns), **invocation.kwargs)
    for migrated, model in ((intents, _INTENTS), (closures, _CLOSURES)):
        assert _contract(migrated) == _contract(model)
        assert {str(item.name) for item in migrated.constraints} == {
            str(item.name) for item in model.constraints
        }
        assert all(len(str(item.name)) <= 63 for item in migrated.constraints)
        assert _indexes(migrated) == _indexes(model)
    assert _indexes(intents) == {
        ("ix_document_upload_intents_project_id", ("project_id",), False, None),
        ("uq_document_upload_intent_pending_path", ("project_id", "folder", "name"),
         True, _PREDICATE),
    }
    assert _indexes(closures) == {
        ("ix_document_upload_closures_project_id", ("project_id",), False, None),
    }
    assert migration.revision == "0042_document_upload_closures"
    assert migration.down_revision == "0041_evaluation_submissions"
    assert [item[0] for item in migration.op.method_calls] == [
        "add_column", "create_check_constraint", "drop_index", "create_index",
        "create_table", "create_index",
    ]
    migration.op.execute.assert_not_called()


def test_closure_has_exact_binding_without_invented_storage_history_or_release_fields() -> None:
    """原対象を RESTRICT で保持し、停止と占用解放/遠端完了の列を混同しない。"""

    contract = _contract(_CLOSURES)
    expected = {
        **dict.fromkeys((
            "id", "upload_intent_id", "organization_id", "project_id", "actor_id", "upload_key",
            "document_id", "requested_by", "request_id", "session_id",
        ), "UUID"),
        "protocol_version": "INTEGER", "binding_checksum": "VARCHAR(71)",
        "closed_at": "TIMESTAMP WITH TIME ZONE",
    }
    assert {name: value[0] for name, value in contract["columns"].items()} == expected
    assert all(not column.nullable and column.server_default is None for column in _CLOSURES.c)
    assert {column.name for column in _CLOSURES.c if column.default is not None} == {"id"}
    assert contract["unique"] == {("upload_intent_id",)}
    assert contract["foreign_keys"] == {
        ("organization_id", "organizations.id", "RESTRICT"),
        ("project_id", "projects.id", "RESTRICT"),
        ("actor_id", "users.id", "RESTRICT"),
        ("requested_by", "users.id", "RESTRICT"),
        ("upload_intent_id", "document_upload_intents.id", "RESTRICT"),
        ("document_id", "document_upload_intents.document_id", "RESTRICT"),
        ("project_id", "document_upload_intents.project_id", "RESTRICT"),
    }
    assert not _CLOSURES.c.session_id.foreign_keys and not _CLOSURES.c.request_id.foreign_keys
    binding = next(
        item for item in _CLOSURES.constraints
        if isinstance(item, sa.ForeignKeyConstraint)
        and item.name == "fk_document_upload_closures_upload_intent"
    )
    assert tuple(binding.column_keys) == ("upload_intent_id", "document_id", "project_id")


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    """実 ORM DDL を局部評価し、PG regex と部分 index だけを明示 seam とする。"""

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function(
            "regexp", 2,
            lambda pattern, value: (
                None if value is None else re.fullmatch(pattern, value) is not None
            ),
        )
        for table, values in (
            ("organizations", (101, 102)), ("projects", (201, 202)), ("users", (301, 302)),
        ):
            connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
            connection.executemany(
                f"INSERT INTO {table} VALUES (?)", [(str(UUID(int=value)),) for value in values],
            )
        dialect = MigrationContext.configure(dialect_name="postgresql").dialect
        for model in (_INTENTS, _CLOSURES):
            ddl = str(CreateTable(model).compile(dialect=sqlite.dialect()))
            connection.execute(ddl.replace(" ~ ", " REGEXP "))
            for index in model.indexes:
                connection.execute(str(CreateIndex(index).compile(dialect=dialect)))
        _insert(connection, _row(publication_closed_at=_AFTER))
        yield connection


def _closure(**changes: object) -> dict[str, object]:
    """元意図と同じ actor/key/document を持つ合成停止記録を作る。"""

    row: dict[str, object] = {
        "id": str(UUID(int=51)), "upload_intent_id": str(UUID(int=1)),
        "organization_id": str(UUID(int=101)), "project_id": str(UUID(int=201)),
        "actor_id": str(UUID(int=301)), "upload_key": str(UUID(int=1001)),
        "document_id": str(UUID(int=4001)), "requested_by": str(UUID(int=301)),
        "request_id": str(UUID(int=61)), "session_id": str(UUID(int=71)),
        "protocol_version": 1, "binding_checksum": "sha256:" + "a" * 64, "closed_at": _AFTER,
    }
    row.update(changes)
    return row


def _save(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    """実 CHECK/FK へ全値を parameter で渡し、固定 bool に置換しない。"""

    columns, values = ", ".join(row), ", ".join(f":{name}" for name in row)
    connection.execute(f"INSERT INTO document_upload_closures ({columns}) VALUES ({values})", row)


@pytest.mark.parametrize("column", [column.name for column in _CLOSURES.c])
def test_closure_rejects_missing_required_fact(database: sqlite3.Connection, column: str) -> None:
    """NULL の三値論理で原要求や監査の欠落を通過させない。"""

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _save(database, _closure(**{column: None}))


@pytest.mark.parametrize("column", [
    "id", "upload_intent_id", "organization_id", "project_id", "actor_id", "upload_key",
    "document_id", "requested_by", "request_id", "session_id",
])
def test_closure_rejects_nil_identities(database: sqlite3.Connection, column: str) -> None:
    """元 ID 不明を nil で補った閉鎖要求を受け入れない。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _save(database, _closure(**{column: str(UUID(int=0))}))


@pytest.mark.parametrize(("column", "value"), [
    ("protocol_version", 0), ("protocol_version", 2),
    ("binding_checksum", ""), ("binding_checksum", "sha256:" + "A" * 64),
    ("binding_checksum", "sha256:" + "a" * 63),
    ("binding_checksum", "sha256:" + "a" * 65),
    ("binding_checksum", "sha512:" + "a" * 64),
    ("requested_by", str(UUID(int=302))),
])
def test_closure_rejects_unknown_protocol_digest_or_replacement_actor(
    database: sqlite3.Connection, column: str, value: object,
) -> None:
    """原 actor 以外の代行や checksum の勝手な補正を DB でも拒否する。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _save(database, _closure(**{column: value}))


@pytest.mark.parametrize("column", ["upload_intent_id", "document_id", "project_id"])
def test_closure_cannot_bind_another_intent_document_or_project(
    database: sqlite3.Connection, column: str,
) -> None:
    """三つ組の RESTRICT FK を独立 ID の FK 三つに弱めない。"""

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _save(database, _closure(**{column: str(UUID(int=999))}))


def test_original_intent_can_have_only_one_closure(database: sqlite3.Connection) -> None:
    """別要求/Session による再読取でも二つ目の監査行は作らない。"""

    _save(database, _closure())
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _save(database, _closure(id=str(UUID(int=52)), request_id=str(UUID(int=62))))


@pytest.mark.parametrize("parent", [
    "organizations", "projects", "users", "document_upload_intents",
])
def test_closure_keeps_original_parents_restricted(
    database: sqlite3.Connection, parent: str,
) -> None:
    """閉鎖済みだからと原帰属を cascade で消すことを許可しない。"""

    _save(database, _closure())
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        database.execute(f"DELETE FROM {parent}")


@pytest.mark.parametrize(("state", "published_at", "closed_at", "allowed"), [
    ("PENDING", None, None, True), ("PENDING", None, _TIME, True),
    ("PENDING", None, _AFTER, True), ("PENDING", None, _BEFORE, False),
    ("PENDING", _TIME, _TIME, False), ("PUBLISHED", _TIME, None, True),
    ("PUBLISHED", _TIME, _TIME, False), ("PUBLISHED", _TIME, _AFTER, False),
])
def test_closed_marker_only_allows_unpublished_originals_after_creation(
    database: sqlite3.Connection, state: str, published_at: str | None,
    closed_at: str | None, allowed: bool,
) -> None:
    """旧 writer の PUBLISHED 更新も CHECK で拒否し、旧 NULL 行はそのまま読む。"""

    values = _row(2, state=state, published_at=published_at, publication_closed_at=closed_at)
    if allowed:
        _insert(database, values)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(database, values)


def test_closed_path_can_be_reused_without_releasing_original_charge(
    database: sqlite3.Connection,
) -> None:
    """同展示 path の新 ID は許すが、旧意図の size と閉鎖監査は残る。"""

    _save(database, _closure())
    _insert(database, _row(2, name="1.txt"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(database, _row(3, name="1.txt"))
    assert database.execute("SELECT sum(size) FROM document_upload_intents").fetchone() == (14,)
    assert database.execute("SELECT count(*) FROM document_upload_closures").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        database.execute(
            "UPDATE document_upload_intents SET state='PUBLISHED', published_at=? WHERE id=?",
            (_AFTER, str(UUID(int=1))),
        )


@pytest.mark.parametrize("retained", ["none", "closure", "marker", "both"])
def test_downgrade_guard_preserves_even_unpaired_closure_facts(
    migration: ModuleType, retained: str,
) -> None:
    """実 server guard 述語を評価し、損壊した片側の事実でも回退を停止する。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE document_upload_intents (publication_closed_at TEXT)")
        database.execute("CREATE TABLE document_upload_closures (id TEXT)")
        database.execute(
            "INSERT INTO document_upload_intents VALUES (?)",
            (_AFTER if retained in {"marker", "both"} else None,),
        )
        if retained in {"closure", "both"}:
            database.execute("INSERT INTO document_upload_closures VALUES (NULL)")

        def execute(statement: str) -> None:
            """PG の lock 実行はせず、DO から取り出した全判定条件を評価する。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Document upload closures must be preserved before downgrade'; "
                r"END IF; END \$\$;", statement,
            )
            assert match is not None
            value = database.execute("SELECT " + match.group(1)).fetchone()
            assert value is not None
            if value[0]:
                raise RuntimeError("preserve closure")

        migration.op.execute.side_effect = execute
        if retained != "none":
            with pytest.raises(RuntimeError, match="preserve closure"):
                migration.downgrade()
            assert [item[0] for item in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            assert [item[0] for item in migration.op.method_calls] == [
                "execute", "execute", "drop_index", "drop_table", "drop_index", "create_index",
                "drop_constraint", "drop_column",
            ]
            predicate = migration.op.create_index.call_args.kwargs["postgresql_where"]
            assert str(predicate) == "state = 'PENDING'"
        assert migration.op.method_calls[0] == call.execute(_LOCK)


@pytest.mark.parametrize("failure_at", [0, 1])
def test_failed_lock_or_retention_check_never_drops_any_fact(
    migration: ModuleType, failure_at: int,
) -> None:
    """停止証明の検査が不明なら未使用と推測せず DDL を中止する。"""

    migration.op.execute.side_effect = [None] * failure_at + [RuntimeError("guard unavailable")]
    with pytest.raises(RuntimeError, match="guard unavailable"):
        migration.downgrade()
    migration.op.drop_index.assert_not_called()
    migration.op.drop_table.assert_not_called()
    migration.op.drop_constraint.assert_not_called()
    migration.op.drop_column.assert_not_called()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_sql_keeps_original_hashes_and_server_side_rollback_protection(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    """offline 出力にも guard を含め、stamp/backfill/監査削除へ逃げない。"""

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output, "target_metadata": Base.metadata},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    getattr(migration, direction)()
    sql = output.getvalue()
    assert not re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", sql)
    assert "CASCADE" not in sql and "DEFAULT" not in sql
    assert "REFERENCES auth_sessions" not in sql and "REFERENCES project_documents" not in sql
    if direction == "upgrade":
        assert "ADD COLUMN publication_closed_at TIMESTAMP WITH TIME ZONE" in sql
        assert "WHERE " + _PREDICATE in sql
        for constraint in _CLOSURES.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                assert f"CONSTRAINT {constraint.name} CHECK ({constraint.sqltext})" in sql
        assert "FOREIGN KEY(upload_intent_id, document_id, project_id)" in sql
        assert "REFERENCES document_upload_intents (id, document_id, project_id)" in sql
        assert "DROP TABLE" not in sql and "DROP COLUMN" not in sql
    else:
        assert _LOCK + ";" in sql
        assert "WHERE publication_closed_at IS NOT NULL" in sql
        assert sql.index(_LOCK) < sql.index("RAISE EXCEPTION") < sql.index("DROP INDEX")
        assert "DROP CONSTRAINT ck_document_upload_intents_publication_closure" in sql
