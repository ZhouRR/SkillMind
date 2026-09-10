"""独立清理要求の DDL・旧資産区別・監査保留を接続なしで検証する。

SQLite は局部の制約評価だけに使い、実 PG の型・lock・transaction の証拠とはしない。
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

from skillmind.db.base import Base
from skillmind.db.models import ProjectDocumentCleanup
from tests.db.test_document_storage_namespace_migration import _migration
from tests.db.test_document_upload_intent_migration import _indexes
from tests.db.test_input_snapshot_migration import _contract

_TABLE = cast(sa.Table, ProjectDocumentCleanup.__table__)
_LOCK = "LOCK TABLE document_blob_cleanups IN ACCESS EXCLUSIVE MODE"
_CHECKSUM = "sha256:" + "a" * 64
_TIME = "2026-09-10T12:00:00+00:00"
_BEFORE = "2026-09-10T11:59:59+00:00"
_AFTER = "2026-09-10T12:00:01+00:00"


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """env.py と既存 DB を使わず 0039 の実 entry point を取得する。"""

    module = _migration("0039_document_blob_cleanups.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def test_cleanup_migration_and_model_preserve_exact_independent_contract(
    migration: ModuleType,
) -> None:
    """旧目録の backfill や 0038 改変をせず、新表の FK/型/index/CHECK を一致させる。"""

    migration.upgrade()
    assert migration.revision == "0039_document_blob_cleanups"
    assert migration.down_revision == "0038_document_upload_intents"
    assert [entry[0] for entry in migration.op.method_calls] == ["create_table", "create_index"]
    name, *elements = migration.op.create_table.call_args.args
    assert name == "document_blob_cleanups"
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("organizations", "projects", "users"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    sa.Table(
        "document_upload_intents", metadata, sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid()), sa.Column("project_id", sa.Uuid()),
        sa.UniqueConstraint("id", "document_id", "project_id"),
    )
    migrated = sa.Table(name, metadata, *elements)
    index_name, table, columns = migration.op.create_index.call_args.args
    assert table == name
    sa.Index(index_name, *(migrated.c[column] for column in columns))
    assert _contract(migrated) == _contract(_TABLE)
    assert {str(item.name) for item in migrated.constraints} == {
        str(item.name) for item in _TABLE.constraints
    }
    assert all(len(str(item.name)) <= 63 for item in migrated.constraints)
    assert _indexes(migrated) == _indexes(_TABLE) == {
        ("ix_document_blob_cleanups_project_id", ("project_id",), False, None),
    }
    migration.op.execute.assert_not_called()


def test_cleanup_shape_has_no_completion_release_or_implicit_history() -> None:
    """元文書/Session の現存と清理監査を分け、完了や占用解放の列を先走って追加しない。"""

    contract = _contract(_TABLE)
    expected_types = {
        **dict.fromkeys((
            "id", "organization_id", "project_id", "requested_by", "document_id", "request_id",
            "session_id", "upload_intent_id", "uploaded_by", "storage_namespace_id",
        ), "UUID"),
        "protocol_version": "INTEGER", "source_protocol": "VARCHAR(32)",
        "folder": "VARCHAR(200)", "name": "VARCHAR(200)", "size": "BIGINT",
        "mime": "VARCHAR(128)", "checksum": "VARCHAR(71)",
        "document_created_at": "TIMESTAMP WITH TIME ZONE", "storage_key": "VARCHAR(512)",
        "storage_descriptor_checksum": "VARCHAR(71)", "storage_is_durable": "BOOLEAN",
        "created_at": "TIMESTAMP WITH TIME ZONE",
    }
    assert {name: value[0] for name, value in contract["columns"].items()} == expected_types
    assert {column.name for column in _TABLE.c if column.nullable} == {"upload_intent_id"}
    assert all(column.server_default is None for column in _TABLE.c)
    assert {column.name for column in _TABLE.c if column.default is not None} == {"id"}
    assert contract["unique"] == {("document_id",)}
    assert contract["foreign_keys"] == {
        ("organization_id", "organizations.id", "RESTRICT"),
        ("project_id", "projects.id", "RESTRICT"),
        ("requested_by", "users.id", "RESTRICT"),
        ("upload_intent_id", "document_upload_intents.id", "RESTRICT"),
        ("document_id", "document_upload_intents.document_id", "RESTRICT"),
        ("project_id", "document_upload_intents.project_id", "RESTRICT"),
    }
    for column in ("session_id", "request_id", "uploaded_by"):
        assert not _TABLE.c[column].foreign_keys
    binding = next(
        item for item in _TABLE.constraints
        if isinstance(item, sa.ForeignKeyConstraint)
        and item.name == "fk_document_blob_cleanups_upload_intent"
    )
    assert tuple(binding.column_keys) == ("upload_intent_id", "document_id", "project_id")
    assert all(
        key.target_fullname.split(".")[0] != "project_documents" for key in _TABLE.foreign_keys
    )


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    """実清理表 DDL を評価し、既存 Document/Session 表を必要としないことも確認する。"""

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function(
            "regexp", 2,
            lambda pattern, value: (
                None if value is None else re.fullmatch(pattern, value) is not None
            ),
        )
        for table, identifier in (("organizations", 101), ("projects", 201), ("users", 301)):
            connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
            connection.execute(f"INSERT INTO {table} VALUES (?)", (str(UUID(int=identifier)),))
        connection.execute(
            "CREATE TABLE document_upload_intents (id TEXT PRIMARY KEY, document_id TEXT, "
            "project_id TEXT, UNIQUE(id, document_id, project_id))"
        )
        connection.execute(
            "INSERT INTO document_upload_intents VALUES (?, ?, ?)",
            (str(UUID(int=1)), str(UUID(int=2)), str(UUID(int=201))),
        )
        ddl = str(CreateTable(_TABLE).compile(dialect=sqlite.dialect()))
        connection.execute(ddl.replace(" ~ ", " REGEXP "))
        for index in _TABLE.indexes:
            connection.execute(str(CreateIndex(index).compile(dialect=sqlite.dialect())))
        yield connection


def _row(**changes: object) -> dict[str, object]:
    """旧 bound 文書由来の合成要求を作り、架空の upload intent へ関連付けない。"""

    row: dict[str, object] = {
        "id": str(UUID(int=11)), "organization_id": str(UUID(int=101)),
        "project_id": str(UUID(int=201)), "requested_by": str(UUID(int=301)),
        "document_id": str(UUID(int=2)), "request_id": str(UUID(int=21)),
        "session_id": str(UUID(int=31)), "upload_intent_id": None, "protocol_version": 1,
        "source_protocol": "LEGACY_UNVERIFIED", "folder": "", "name": "note.txt", "size": 5,
        "mime": "text/plain", "checksum": _CHECKSUM, "uploaded_by": str(UUID(int=401)),
        "document_created_at": _TIME, "storage_key": "original/document/key",
        "storage_namespace_id": str(UUID(int=501)), "storage_descriptor_checksum": _CHECKSUM,
        "storage_is_durable": True, "created_at": _AFTER,
    }
    row.update(changes)
    return row


def _insert(database: sqlite3.Connection, row: dict[str, object]) -> None:
    """全値を parameter として保存し、実 CHECK/unique/FK の結果を観測する。"""

    columns, values = ", ".join(row), ", ".join(f":{name}" for name in row)
    database.execute(f"INSERT INTO document_blob_cleanups ({columns}) VALUES ({values})", row)


@pytest.mark.parametrize("column", [column.name for column in _TABLE.c if not column.nullable])
def test_cleanup_requires_all_original_facts(database: sqlite3.Connection, column: str) -> None:
    """三値論理の NULL 通過で、元 byte/namespace/DELETE 要求の欠落を保存しない。"""

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _insert(database, _row(**{column: None}))


@pytest.mark.parametrize("column", [
    "document_id", "request_id", "session_id", "uploaded_by", "storage_namespace_id",
])
def test_cleanup_rejects_nil_original_identities(database: sqlite3.Connection, column: str) -> None:
    """元 ID 不明を nil へ置き換えて清理可能と表示することを防ぐ。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(**{column: str(UUID(int=0))}))


@pytest.mark.parametrize("column", ["checksum", "storage_descriptor_checksum"])
@pytest.mark.parametrize("value", [
    "", "sha256:" + "A" * 64, "sha256:" + "g" * 64, "sha256:" + "a" * 63,
    "sha256:" + "a" * 65, "sha512:" + "a" * 64,
])
def test_cleanup_requires_exact_lowercase_sha256_shape(
    database: sqlite3.Connection, column: str, value: str,
) -> None:
    """原 byte と storage descriptor の摘要を別形式へ黙って補正しない。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(**{column: value}))


@pytest.mark.parametrize(("column", "value"), [
    ("name", ""), ("storage_key", ""), ("mime", ""), ("size", 0), ("size", -1),
    ("protocol_version", 0), ("protocol_version", 2), ("created_at", _BEFORE),
])
def test_cleanup_rejects_invalid_or_invented_history(
    database: sqlite3.Connection, column: str, value: object,
) -> None:
    """清理要求を原文書より前に置かず、空対象や負数占用で削除を通さない。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(**{column: value}))


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("protocol", ["LEGACY_UNVERIFIED", "UPLOAD_INTENT_V1", "", "UNKNOWN"])
def test_source_protocol_exactly_distinguishes_real_intent_and_unverified_legacy(
    database: sqlite3.Connection, linked: bool, protocol: str,
) -> None:
    """旧行の原 PUT を後付けで認定せず、新方式も関連意図なしで名乗れない。"""

    row = _row(upload_intent_id=str(UUID(int=1)) if linked else None, source_protocol=protocol)
    allowed = protocol == ("UPLOAD_INTENT_V1" if linked else "LEGACY_UNVERIFIED")
    if allowed:
        _insert(database, row)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(database, row)


def test_legacy_cleanup_preserves_root_false_and_large_size_without_document_or_session(
    database: sqlite3.Connection,
) -> None:
    """有効な旧 byte identity は upload 履歴なしで保存でき、占用を 32 bit に縮めない。"""

    _insert(database, _row(folder="", storage_is_durable=False, size=2**40, created_at=_TIME))
    assert database.execute(
        "SELECT folder, storage_is_durable, size, upload_intent_id FROM document_blob_cleanups"
    ).fetchone() == ("", 0, 2**40, None)


def test_one_document_has_only_one_independent_cleanup_request(
    database: sqlite3.Connection,
) -> None:
    """新しい DELETE request ID や actor で同じ原文書の清理占用を重複保存しない。"""

    _insert(database, _row())
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(database, _row(id=str(UUID(int=12)), request_id=str(UUID(int=22))))


@pytest.mark.parametrize("column", ["upload_intent_id", "document_id", "project_id"])
def test_linked_cleanup_must_match_original_intent_document_and_project(
    database: sqlite3.Connection, column: str,
) -> None:
    """三つ組の FK が別文書/Project の公開意図への誤関連を拒否する。"""

    row = _row(upload_intent_id=str(UUID(int=1)), source_protocol="UPLOAD_INTENT_V1")
    row[column] = str(UUID(int=999))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(database, row)


@pytest.mark.parametrize("parent", [
    "organizations", "projects", "users", "document_upload_intents",
])
def test_cleanup_audit_restricts_parent_removal(database: sqlite3.Connection, parent: str) -> None:
    """清理要求を Project/actor/原意図と一緒に cascade して監査と占用を失わない。"""

    _insert(database, _row(upload_intent_id=str(UUID(int=1)), source_protocol="UPLOAD_INTENT_V1"))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        database.execute(f"DELETE FROM {parent}")


@pytest.mark.parametrize("retained", [False, True])
def test_downgrade_actual_guard_rejects_any_cleanup_before_drop(
    migration: ModuleType, retained: bool,
) -> None:
    """原 DELETE 成功や state で絞らず、どんな保持行も排他 lock 後の server 判定で守る。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE document_blob_cleanups (id TEXT)")
        if retained:
            database.execute("INSERT INTO document_blob_cleanups VALUES (NULL)")

        def execute(statement: str) -> None:
            """実 DO 述語を評価し、mock の固定 bool で誤った WHERE を隠さない。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Document blob cleanup records must be preserved before downgrade'; "
                r"END IF; END \$\$;", statement,
            )
            assert match is not None
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None
            if result[0]:
                raise RuntimeError("preserve cleanup")

        migration.op.execute.side_effect = execute
        if retained:
            with pytest.raises(RuntimeError, match="preserve cleanup"):
                migration.downgrade()
            assert [entry[0] for entry in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            assert [entry[0] for entry in migration.op.method_calls] == [
                "execute", "execute", "drop_index", "drop_table",
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT count(*) FROM document_blob_cleanups").fetchone() == (
            int(retained),
        )


@pytest.mark.parametrize("failure_at", [0, 1])
def test_downgrade_failed_lock_or_guard_never_drops_audit(
    migration: ModuleType, failure_at: int,
) -> None:
    """保護判定不能を空表と扱わず、失敗時は制約/index/行をそのまま残す。"""

    migration.op.execute.side_effect = [None] * failure_at + [RuntimeError("guard unavailable")]
    with pytest.raises(RuntimeError, match="guard unavailable"):
        migration.downgrade()
    migration.op.drop_index.assert_not_called()
    migration.op.drop_table.assert_not_called()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_postgresql_sql_keeps_cleanup_guard_and_never_rewrites_legacy_rows(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    """offline SQL でも保持 guard を必須とし、既存目録/意図/namespace の DML を出さない。"""

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
    if direction == "upgrade":
        for check in _TABLE.constraints:
            if isinstance(check, sa.CheckConstraint):
                assert f"CONSTRAINT {check.name} CHECK ({check.sqltext})" in sql
        assert "CREATE INDEX ix_document_blob_cleanups_project_id" in sql
        assert "FOREIGN KEY(upload_intent_id, document_id, project_id)" in sql
        assert "REFERENCES document_upload_intents (id, document_id, project_id)" in sql
        assert "REFERENCES project_documents" not in sql and "REFERENCES auth_sessions" not in sql
        assert "DROP" not in sql
    else:
        assert _LOCK + ";" in sql
        assert "IF EXISTS (SELECT 1 FROM document_blob_cleanups) THEN RAISE EXCEPTION" in sql
        assert sql.index(_LOCK) < sql.index("RAISE EXCEPTION") < sql.index("DROP INDEX")
