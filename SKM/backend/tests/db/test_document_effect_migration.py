"""成果 upload の migration/model 同期、段階 CHECK と元文書 FK を検証する。"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from io import StringIO
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from skillmind.db.base import Base
from skillmind.db.models import ProjectDocument, ProjectDocumentEffectUpload
from tests.db.test_input_snapshot_migration import _contract


def migration():
    """環境設定や DB 接続を開かず、実 revision の operation を取得する。"""
    path = (
        Path(__file__).resolve().parents[2] / "migrations/versions/0045_document_effect_uploads.py"
    )
    spec = importlib.util.spec_from_file_location("document_effect_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_matches_model_and_adds_only_nullable_document_origin(monkeypatch):
    """履歴を backfill せず、原 Effect/FK と全段階制約を model と同じ形に作る。"""
    module = migration()
    monkeypatch.setattr(module, "op", Mock())
    module.op.f.side_effect = sa.schema.conv
    module.upgrade()
    assert module.down_revision == "0044_interpretation_requests"
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("organizations", "projects", "users", "runs", "effect_executions"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    name, *items = module.op.create_table.call_args.args
    table = sa.Table(name, metadata, *items)
    for call in module.op.create_index.call_args_list:
        name, target, columns = call.args
        assert target == table.name
        sa.Index(name, *(table.c[column] for column in columns), **call.kwargs)
    assert _contract(table) == _contract(ProjectDocumentEffectUpload.__table__)
    assert {item.name for item in table.constraints} == {
        item.name for item in ProjectDocumentEffectUpload.__table__.constraints
    }
    target, column = module.op.add_column.call_args.args
    assert target == "project_documents" and column.name == "effect_upload_id" and column.nullable
    assert column.default is None and column.server_default is None
    assert module.op.create_foreign_key.call_args.args[3:] == (
        ["effect_upload_id", "id", "project_id"],
        ["id", "document_id", "project_id"],
    )
    assert module.op.create_foreign_key.call_args.kwargs == {"ondelete": "RESTRICT"}
    module.op.execute.assert_not_called()


def test_downgrade_locks_and_refuses_retained_records_before_dropping_columns(monkeypatch):
    """公開済みや未送信も監査として残し、降級で無料の孤立 object を作らない。"""
    module = migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    monkeypatch.setattr(module, "op", Operations(context))
    module.downgrade()
    sql = output.getvalue()
    assert (
        sql.index("LOCK TABLE document_effect_uploads, project_documents")
        < sql.index("IF EXISTS (SELECT 1 FROM document_effect_uploads)")
        < sql.index("DROP COLUMN effect_upload_id")
        < sql.index("DROP TABLE document_effect_uploads")
    )
    assert "WHERE" not in sql


@pytest.fixture
def database():
    """SQLite に実 DDL の regex だけを適合する。PG lock/型の証明には使わない。"""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.create_function(
        "regexp",
        2,
        lambda pattern, value: None if value is None else re.fullmatch(pattern, value) is not None,
    )
    for name in ("organizations", "projects", "users", "runs", "effect_executions"):
        connection.execute(f"CREATE TABLE {name} (id TEXT PRIMARY KEY)")
        connection.execute(f"INSERT INTO {name} VALUES (?)", (str(UUID(int=1)),))
    connection.execute(
        "CREATE TABLE document_upload_intents (id TEXT,document_id TEXT,project_id TEXT, "
        "UNIQUE(id,document_id,project_id))"
    )
    for model in (ProjectDocumentEffectUpload, ProjectDocument):
        ddl = str(CreateTable(model.__table__).compile(dialect=sqlite.dialect())).replace(
            " ~ ", " REGEXP "
        )
        connection.execute(ddl)
    try:
        yield connection
    finally:
        connection.close()


def row(**overrides):
    """SQL CHECK の NULL/時刻境界を調べる合成の元予約を作る。"""
    value = dict(
        id=str(UUID(int=2)),
        organization_id=str(UUID(int=1)),
        project_id=str(UUID(int=1)),
        run_id=str(UUID(int=1)),
        effect_id=str(UUID(int=1)),
        actor_id=str(UUID(int=1)),
        artifact_ref="art_fixture",
        document_id=str(UUID(int=3)),
        protocol_version=1,
        request_checksum="sha256:" + "a" * 64,
        folder="results",
        name="source.md",
        bucket="fixture",
        storage_key="fixture/source.md",
        storage_namespace_id=str(UUID(int=4)),
        storage_descriptor_checksum="sha256:" + "b" * 64,
        storage_is_durable=True,
        size=5,
        mime="text/markdown",
        checksum="sha256:" + "c" * 64,
        state="RESERVED",
        put_owner_id=None,
        etag=None,
        version_id=None,
        created_at="2026-09-11T00:00:00Z",
        sent_at=None,
        verified_at=None,
        published_at=None,
    )
    return {**value, **overrides}


def insert(connection, value):
    """field 名は固定 fixture 由来、値はすべて bind parameter で渡す。"""
    connection.execute(
        f"INSERT INTO document_effect_uploads ({','.join(value)}) "
        f"VALUES ({','.join(':' + key for key in value)})",
        value,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "SENT", "put_owner_id": str(UUID(int=9))},
        {"state": "VERIFIED", "put_owner_id": str(UUID(int=9)), "etag": '"original"'},
        {"state": "PUBLISHED", "put_owner_id": str(UUID(int=9)), "etag": '"original"'},
        {"state": "UNRECOGNIZED"},
        {"size": 0},
        {"size": 1_048_577},
        {"storage_is_durable": False},
        {"etag": '"unexpected"'},
        {"sent_at": "2026-09-11T00:00:00Z"},
    ],
)
def test_invalid_stage_and_null_times_cannot_pass_sql_check(database, overrides):
    """SQL の三値論理で未核対の SENT/VERIFIED/PUBLISHED を通過させない。"""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(database, row(**overrides))


def test_original_effect_path_object_and_document_id_are_unique_and_retained(database):
    """元行を削除して再生成したり、別の予約を同じ path/object に重ねたりできない。"""
    original = row()
    insert(database, original)
    for changed in ({"id": str(UUID(int=10))}, {"document_id": str(UUID(int=10))}):
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            insert(database, {**original, **changed})
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        database.execute("DELETE FROM effect_executions")
