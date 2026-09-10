"""文書の namespace DDL、NULL 境界と回退 guard を接続なしで検証する。

SQLite は CHECK の三値論理と guard 述語だけに使う。PG 型/正規表現/lock の実機検証ではない。
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from contextlib import closing
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import Mock, call
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.elements import conv

from skillmind.db.base import Base
from skillmind.db.models import ProjectDocument
from tests.db.test_input_snapshot_migration import _contract

_TABLE = cast(sa.Table, ProjectDocument.__table__)
_COLUMNS = ("storage_namespace_id", "storage_descriptor_checksum", "storage_is_durable")
_LOCK = "LOCK TABLE project_documents IN ACCESS EXCLUSIVE MODE"
_CHECK_NAME = "ck_project_documents_storage_namespace_binding"
_NAMESPACE = str(UUID(int=1))
_CHECKSUM = "sha256:" + "a" * 64


def _migration(filename: str) -> ModuleType:
    """env.py や実設定を評価せず、指定 migration の関数だけを取得する。"""

    path = Path(__file__).resolve().parents[2] / "migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(path.stem + "_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """DDL 呼出だけを記録し、既存 DB/設定への接続を許可しない。"""

    module = _migration("0037_document_storage_namespace.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def _check() -> str:
    """実 ORM の唯一の帰属制約を使い、test 用の別業務条件を作らない。"""

    checks = [
        str(item.sqltext) for item in _TABLE.constraints
        if isinstance(item, sa.CheckConstraint) and item.name == _CHECK_NAME
    ]
    assert len(checks) == 1
    return checks[0]


def test_0037_matches_original_0014_plus_nullable_columns_without_history_rewrites(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0014 の既存形を保ち、後続 0038 の列/FK を除いた 0037 到達形と照合する。"""

    original = _migration("0014_project_documents.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(original, "op", operations)
    original.upgrade()
    migration.upgrade()
    assert migration.revision == "0037_document_storage_namespace"
    assert migration.down_revision == "0036_schedule_occurrences"
    assert [entry[0] for entry in migration.op.method_calls] == [
        "add_column", "add_column", "add_column", "create_check_constraint",
    ]
    name, *elements = operations.create_table.call_args.args
    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    migrated = sa.Table(name, metadata, *elements)
    old_names = set(migrated.columns.keys())
    for operation in migration.op.add_column.call_args_list:
        table, column = operation.args
        assert table == "project_documents" and column.name in _COLUMNS
        assert column.nullable and column.default is None and column.server_default is None
        migrated.append_column(column)
    check_name, table, condition = migration.op.create_check_constraint.call_args.args
    assert table == "project_documents"
    migrated.append_constraint(sa.CheckConstraint(condition, name=check_name))
    assert set(migrated.columns.keys()) - old_names == set(_COLUMNS)
    # 後続の意図関連を除く。0037 の DDL 自体を新しい ORM に合わせて改変しない。
    expected = _contract(_TABLE)
    expected["columns"].pop("upload_intent_id")
    expected["foreign_keys"] = {
        key for key in expected["foreign_keys"] if not key[1].startswith("document_upload_intents.")
    }
    assert _contract(migrated) == expected
    assert {str(item.name) for item in migrated.constraints} == {
        str(item.name) for item in _TABLE.constraints
        if item.name != "fk_project_documents_upload_intent"
    }
    assert {
        (str(item.name), tuple(column.name for column in item.columns)) for item in _TABLE.indexes
    } == {
        (operation.args[0], tuple(operation.args[2]))
        for operation in operations.create_index.call_args_list
    }
    migration.op.execute.assert_not_called()


def test_namespace_columns_have_exact_nullable_types_and_no_inferred_defaults() -> None:
    """UUID/摘要/Boolean の形を固定し、None と False や現在の接続先を混同しない。"""

    assert isinstance(_TABLE.c.storage_namespace_id.type, sa.Uuid)
    assert isinstance(_TABLE.c.storage_descriptor_checksum.type, sa.String)
    assert _TABLE.c.storage_descriptor_checksum.type.length == 71
    assert isinstance(_TABLE.c.storage_is_durable.type, sa.Boolean)
    for name in _COLUMNS:
        column = _TABLE.c[name]
        assert column.nullable and column.default is None and column.server_default is None
    check = _check()
    assert "storage_namespace_id <> '00000000-0000-0000-0000-000000000000'" in check
    assert "storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$'" in check


@pytest.mark.parametrize("namespace", [None, _NAMESPACE, str(UUID(int=0))])
@pytest.mark.parametrize("checksum", [
    None, _CHECKSUM, "", "sha256:" + "A" * 64, "sha256:" + "a" * 63,
    "sha256:" + "a" * 65, "sha256:" + "g" * 64, "sha512:" + "a" * 64,
])
@pytest.mark.parametrize("durable", [None, False, True])
def test_binding_check_accepts_only_complete_valid_identity_or_all_null(
    namespace: str | None, checksum: str | None, durable: bool | None,
) -> None:
    """部分 NULL が CHECK の UNKNOWN で通過せず、非永続の False も完全な関連値とする。"""

    allowed = (namespace is None and checksum is None and durable is None) or (
        namespace == _NAMESPACE and checksum == _CHECKSUM and durable is not None
    )
    with closing(sqlite3.connect(":memory:")) as database:
        # 正規表現演算子だけを SQLite の等価 seam へ写す。PG の型/regex 実装は別途検証する。
        database.create_function(
            "regexp", 2,
            lambda pattern, value: (
                None if value is None else re.fullmatch(pattern, value) is not None
            ),
        )
        check = _check().replace(" ~ ", " REGEXP ")
        database.execute(
            "CREATE TABLE bindings (storage_namespace_id TEXT, storage_descriptor_checksum TEXT, "
            "storage_is_durable INTEGER, CHECK (" + check + "))"
        )
        if allowed:
            database.execute(
                "INSERT INTO bindings VALUES (?, ?, ?)", (namespace, checksum, durable)
            )
        else:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                database.execute(
                    "INSERT INTO bindings VALUES (?, ?, ?)", (namespace, checksum, durable)
                )
        assert database.execute("SELECT count(*) FROM bindings").fetchone() == (int(allowed),)


def test_upgrade_keeps_existing_identity_key_hash_and_unbound_old_writers(
    migration: ModuleType,
) -> None:
    """実 ADD COLUMN の結果を旧行に適用し、backfill/default による保存先の推測を防ぐ。"""

    migration.upgrade()
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE project_documents (id TEXT, storage_key TEXT, checksum TEXT)"
        )
        database.execute("INSERT INTO project_documents VALUES ('old-id', 'old-key', 'old-hash')")
        for operation in migration.op.add_column.call_args_list:
            table, column = operation.args
            ddl = str(CreateColumn(column).compile(dialect=sqlite.dialect()))
            database.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        database.execute(
            "INSERT INTO project_documents (id, storage_key, checksum) "
            "VALUES ('old-writer', 'same-key', 'same-hash')"
        )
        assert database.execute("SELECT * FROM project_documents ORDER BY id").fetchall() == [
            ("old-id", "old-key", "old-hash", None, None, None),
            ("old-writer", "same-key", "same-hash", None, None, None),
        ]
    migration.op.execute.assert_not_called()


@pytest.mark.parametrize("namespace", [None, _NAMESPACE, str(UUID(int=0))])
@pytest.mark.parametrize("checksum", [None, _CHECKSUM, ""])
@pytest.mark.parametrize("durable", [None, False, True])
def test_downgrade_actual_guard_rejects_any_namespace_information_before_drop(
    migration: ModuleType, namespace: str | None, checksum: str | None, durable: bool | None,
) -> None:
    """破損した部分値や False も保護し、全 NULL の旧行だけを列破棄可能と判定する。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE project_documents (id TEXT, storage_namespace_id TEXT, "
            "storage_descriptor_checksum TEXT, storage_is_durable INTEGER)"
        )
        values = ("original-document", namespace, checksum, durable)
        database.execute("INSERT INTO project_documents VALUES (?, ?, ?, ?)", values)

        def execute(statement: str) -> None:
            """lock は順序で検査し、server DO の実 EXISTS 述語はローカル SQL で評価する。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Document storage namespace bindings must be preserved before downgrade'; "
                r"END IF; END \$\$;", statement,
            )
            assert match is not None, "Unexpected SQL or history rewrite"
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None and result[0] in (0, 1)
            if result[0]:
                raise RuntimeError("preserve namespace binding")

        migration.op.execute.side_effect = execute
        if any(value is not None for value in (namespace, checksum, durable)):
            with pytest.raises(RuntimeError, match="preserve namespace binding"):
                migration.downgrade()
            assert [entry[0] for entry in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            migration.op.drop_constraint.assert_called_once_with(
                _CHECK_NAME, "project_documents", type_="check"
            )
            assert [entry.args for entry in migration.op.drop_column.call_args_list] == [
                ("project_documents", name) for name in reversed(_COLUMNS)
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM project_documents").fetchall() == [values]


@pytest.mark.parametrize("failure_at", [0, 1])
def test_downgrade_lock_or_guard_failure_stops_without_dropping(
    migration: ModuleType, failure_at: int,
) -> None:
    """lock/保護判定が失敗しても、未使用という仮定で帰属列を落とさない。"""

    migration.op.execute.side_effect = [None] * failure_at + [RuntimeError("check unavailable")]
    with pytest.raises(RuntimeError, match="check unavailable"):
        migration.downgrade()
    assert migration.op.method_calls[0] == call.execute(_LOCK)
    migration.op.drop_constraint.assert_not_called()
    migration.op.drop_column.assert_not_called()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_postgresql_offline_sql_retains_binding_check_and_server_downgrade_guard(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    """接続なしの実 PG compiler でも guard を省略せず、旧 metadata の DML は生成しない。"""

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
        for name, sql_type in zip(_COLUMNS, ("UUID", "VARCHAR(71)", "BOOLEAN"), strict=True):
            assert f"ALTER TABLE project_documents ADD COLUMN {name} {sql_type};" in sql
        assert f"ADD CONSTRAINT {_CHECK_NAME} CHECK ({_check()});" in sql
        assert "DROP" not in sql
    else:
        assert _LOCK + ";" in sql and "DO $$ BEGIN IF EXISTS" in sql
        assert "RAISE EXCEPTION" in sql
        assert sql.index(_LOCK) < sql.index("RAISE EXCEPTION") < sql.index("DROP CONSTRAINT")
        for name in _COLUMNS:
            assert f"{name} IS NOT NULL" in sql
            assert f"DROP COLUMN {name};" in sql
