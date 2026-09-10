"""持続 upload intent の DDL・制約・監査保留を実サービスなしで検証する。

SQLite は制約/guard 述語の局部証拠であり、PG の UUID/regex/lock と同時実行は別途検証する。
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
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable
from sqlalchemy.sql.elements import conv

from projectmind.db.base import Base
from projectmind.db.models import ProjectDocument, ProjectDocumentUpload
from tests.db.test_document_storage_namespace_migration import _migration
from tests.db.test_input_snapshot_migration import _contract

_INTENTS = cast(sa.Table, ProjectDocumentUpload.__table__)
_DOCUMENTS = cast(sa.Table, ProjectDocument.__table__)
_LOCK = "LOCK TABLE project_documents, document_upload_intents IN ACCESS EXCLUSIVE MODE"
_CHECKSUM = "sha256:" + "a" * 64
_TIME = "2026-09-10T12:00:00+00:00"
_BEFORE = "2026-09-10T11:59:59+00:00"
_AFTER = "2026-09-10T12:00:01+00:00"


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """env.py や設定を読まず、0038 の実 entry point と DDL 呼出を取得する。"""

    module = _migration("0038_document_upload_intents.py")
    operations = Mock()
    operations.f.side_effect = conv
    monkeypatch.setattr(module, "op", operations)
    return module


def _indexes(table: sa.Table) -> set[tuple[str, tuple[str, ...], bool, str | None]]:
    """部分一意 index の対象列と述語を含め、migration と model を照合する。"""

    return {
        (
            str(index.name), tuple(column.name for column in index.columns), bool(index.unique),
            str(index.dialect_options["postgresql"]["where"])
            if index.dialect_options["postgresql"]["where"] is not None else None,
        )
        for index in table.indexes
    }


def test_0038_and_complete_document_migration_chain_match_model(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0014→0037→0038 を実 DDL から合成し、旧列を含む最新の二表契約と一致させる。"""

    metadata = sa.MetaData(naming_convention=Base.metadata.naming_convention)
    for parent in ("organizations", "projects", "users"):
        sa.Table(parent, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    original = _migration("0014_project_documents.py")
    namespace = _migration("0037_document_storage_namespace.py")
    for module in (original, namespace):
        operations = Mock()
        operations.f.side_effect = conv
        monkeypatch.setattr(module, "op", operations)
        module.upgrade()
    name, *elements = original.op.create_table.call_args.args
    documents = sa.Table(name, metadata, *elements)
    for operation in namespace.op.add_column.call_args_list:
        documents.append_column(operation.args[1])
    check_name, _, condition = namespace.op.create_check_constraint.call_args.args
    documents.append_constraint(sa.CheckConstraint(condition, name=check_name))
    migration.upgrade()
    name, *elements = migration.op.create_table.call_args.args
    intents = sa.Table(name, metadata, *elements)
    for operation in migration.op.create_index.call_args_list:
        index_name, table, columns = operation.args
        assert table == intents.name
        sa.Index(index_name, *(intents.c[column] for column in columns), **operation.kwargs)
    table, column = migration.op.add_column.call_args.args
    assert table == documents.name and column.name == "upload_intent_id"
    assert column.nullable and column.default is None and column.server_default is None
    documents.append_column(column)
    fk_name, source, target, local, remote = migration.op.create_foreign_key.call_args.args
    assert source == documents.name and target == intents.name
    documents.append_constraint(sa.ForeignKeyConstraint(
        local, [f"{target}.{column}" for column in remote], name=fk_name,
        **migration.op.create_foreign_key.call_args.kwargs,
    ))
    assert migration.revision == "0038_document_upload_intents"
    assert migration.down_revision == "0037_document_storage_namespace"
    assert [entry[0] for entry in migration.op.method_calls] == [
        "create_table", "create_index", "create_index", "add_column", "create_foreign_key",
    ]
    for migrated, model in ((intents, _INTENTS), (documents, _DOCUMENTS)):
        model_contract = _contract(model)
        constraint_names = {str(item.name) for item in model.constraints}
        if model is _INTENTS:
            # 0042 の追加は別回帰で合成し、発行済み 0038 の DDL を最新 model に合わせて変えない。
            model_contract["columns"].pop("publication_closed_at")
            model_contract["checks"].remove(
                "publication_closed_at IS NULL OR (state = 'PENDING' AND published_at IS NULL "
                "AND publication_closed_at >= created_at)",
            )
            constraint_names.remove("ck_document_upload_intents_publication_closure")
        assert _contract(migrated) == model_contract
        assert {str(item.name) for item in migrated.constraints} == constraint_names
        assert all(len(str(item.name)) <= 63 for item in migrated.constraints)
    assert _indexes(intents) == {
        ("ix_document_upload_intents_project_id", ("project_id",), False, None),
        (
            "uq_document_upload_intent_pending_path", ("project_id", "folder", "name"),
            True, "state = 'PENDING'",
        ),
    }
    migration.op.execute.assert_not_called()


def test_intent_contract_has_exact_identity_types_and_no_release_or_inferred_defaults() -> None:
    """占用を常に intent.size で保持し、Session の現存や既存 metadata に依存させない。"""

    contract = _contract(_INTENTS)
    assert contract["foreign_keys"] == {
        ("organization_id", "organizations.id", "RESTRICT"),
        ("project_id", "projects.id", "RESTRICT"),
        ("actor_id", "users.id", "RESTRICT"),
    }
    assert contract["unique"] == {
        ("organization_id", "project_id", "actor_id", "upload_key"), ("document_id",),
        ("storage_namespace_id", "storage_key"), ("id", "document_id", "project_id"),
    }
    expected_types = {
        **dict.fromkeys((
            "id", "organization_id", "project_id", "actor_id", "upload_key",
            "original_request_id", "original_session_id", "document_id", "storage_namespace_id",
        ), "UUID"),
        "protocol_version": "INTEGER", "request_checksum": "VARCHAR(71)",
        "folder": "VARCHAR(200)", "name": "VARCHAR(200)", "storage_key": "VARCHAR(512)",
        "storage_descriptor_checksum": "VARCHAR(71)", "storage_is_durable": "BOOLEAN",
        "write_protocol": "VARCHAR(32)", "size": "BIGINT", "mime": "VARCHAR(128)",
        "checksum": "VARCHAR(71)", "state": "VARCHAR(16)",
        **dict.fromkeys((
            "created_at", "published_at", "cleanup_requested_at", "publication_closed_at",
        ),
                       "TIMESTAMP WITH TIME ZONE"),
    }
    assert {name: value[0] for name, value in contract["columns"].items()} == expected_types
    assert {column.name for column in _INTENTS.c if column.nullable} == {
        "published_at", "cleanup_requested_at", "publication_closed_at",
    }
    assert all(column.server_default is None for column in _INTENTS.c)
    assert {column.name for column in _INTENTS.c if column.default is not None} == {"id"}
    assert not _INTENTS.c.original_session_id.foreign_keys
    binding = [
        item for item in _DOCUMENTS.constraints
        if isinstance(item, sa.ForeignKeyConstraint)
        and item.name == "fk_project_documents_upload_intent"
    ]
    assert len(binding) == 1 and binding[0].ondelete == "RESTRICT"
    assert tuple(binding[0].column_keys) == ("upload_intent_id", "id", "project_id")
    assert tuple(element.target_fullname for element in binding[0].elements) == (
        "document_upload_intents.id", "document_upload_intents.document_id",
        "document_upload_intents.project_id",
    )


@pytest.fixture
def database() -> Iterator[sqlite3.Connection]:
    """実 ORM DDL を SQLite に写し、regex 演算子だけを局部 seam に交換する。"""

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function(
            "regexp", 2,
            lambda pattern, value: (
                None if value is None else re.fullmatch(pattern, value) is not None
            ),
        )
        for table, identifiers in (
            ("organizations", (101, 102)), ("projects", (201, 202)), ("users", (301, 302)),
        ):
            connection.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
            connection.executemany(
                f"INSERT INTO {table} VALUES (?)",
                [(str(UUID(int=value)),) for value in identifiers],
            )
        for model in (_INTENTS, _DOCUMENTS):
            ddl = str(CreateTable(model).compile(dialect=sqlite.dialect()))
            ddl = ddl.replace(" ~ ", " REGEXP ")
            connection.execute(ddl)
        # SQLite compiler は PG 専用 predicate を落とすため、実 PG index DDL をそのまま評価する。
        dialect = MigrationContext.configure(dialect_name="postgresql").dialect
        for index in _INTENTS.indexes:
            connection.execute(str(CreateIndex(index).compile(dialect=dialect)))
        yield connection


def _row(number: int = 1, /, **changes: object) -> dict[str, object]:
    """実 identity/checksum 形式の合成意図を作り、秘密や実 DB の参照を使わない。"""

    row: dict[str, object] = {
        "id": str(UUID(int=number)), "organization_id": str(UUID(int=101)),
        "project_id": str(UUID(int=201)), "actor_id": str(UUID(int=301)),
        "upload_key": str(UUID(int=1000 + number)),
        "original_request_id": str(UUID(int=2000 + number)),
        "original_session_id": str(UUID(int=3000 + number)), "protocol_version": 1,
        "request_checksum": _CHECKSUM, "document_id": str(UUID(int=4000 + number)),
        "folder": "", "name": f"{number}.txt", "storage_key": f"documents/{number}",
        "storage_namespace_id": str(UUID(int=501)), "storage_descriptor_checksum": _CHECKSUM,
        "storage_is_durable": True, "write_protocol": "UNCONDITIONAL_V1", "size": 7,
        "mime": "text/plain", "checksum": _CHECKSUM, "state": "PENDING", "created_at": _TIME,
        "published_at": None, "cleanup_requested_at": None,
    }
    row.update(changes)
    return row


def _insert(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    """値を SQL へ補間せず、実表の全列へ合成意図を挿入する。"""

    columns = ", ".join(row)
    values = ", ".join(f":{column}" for column in row)
    connection.execute(f"INSERT INTO document_upload_intents ({columns}) VALUES ({values})", row)


@pytest.mark.parametrize("column", [column.name for column in _INTENTS.c if not column.nullable])
def test_intent_rejects_missing_required_fact(database: sqlite3.Connection, column: str) -> None:
    """CHECK の UNKNOWN に頼らず、元要求・帰属・占用・作成時刻を NOT NULL で守る。"""

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        _insert(database, _row(**{column: None}))


@pytest.mark.parametrize("column", [
    "upload_key", "original_request_id", "original_session_id", "document_id",
    "storage_namespace_id",
])
def test_intent_rejects_nil_frozen_identities(database: sqlite3.Connection, column: str) -> None:
    """不明な元要求や保存先を nil UUID で補った意図は永続化しない。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(**{column: str(UUID(int=0))}))


@pytest.mark.parametrize("column", ["request_checksum", "storage_descriptor_checksum", "checksum"])
@pytest.mark.parametrize("value", [
    "", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 65,
    "sha256:" + "g" * 64, "sha512:" + "a" * 64,
])
def test_intent_rejects_invalid_checksum_shape(
    database: sqlite3.Connection, column: str, value: str,
) -> None:
    """三種類の checksum は同じ小文字 SHA-256 外形で固定する。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(**{column: value}))


@pytest.mark.parametrize(("column", "value"), [
    ("protocol_version", 0), ("protocol_version", 2), ("write_protocol", "CONDITIONAL_V2"),
    ("write_protocol", ""), ("name", ""), ("storage_key", ""), ("mime", ""),
    ("size", 0), ("size", -1), ("state", "FAILED"), ("state", ""),
])
def test_intent_rejects_unsupported_protocol_or_invalid_required_value(
    database: sqlite3.Connection, column: str, value: object,
) -> None:
    """未設計状態やゼロ占用へ落として元 write の監査を解放しない。"""

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _insert(database, _row(**{column: value}))


@pytest.mark.parametrize("state", ["PENDING", "PUBLISHED"])
@pytest.mark.parametrize("published", [None, _BEFORE, _TIME, _AFTER])
@pytest.mark.parametrize("cleanup", [None, _BEFORE, _TIME, _AFTER])
def test_publication_and_cleanup_timestamps_form_a_valid_history(
    database: sqlite3.Connection, state: str, published: str | None, cleanup: str | None,
) -> None:
    """PENDING の日時や NULL published を拒否し、清理要求は公開より前に置かない。"""

    allowed = (state == "PENDING" and published is None and cleanup is None) or (
        state == "PUBLISHED" and published is not None and published >= _TIME
        and (cleanup is None or cleanup >= published)
    )
    row = _row(state=state, published_at=published, cleanup_requested_at=cleanup)
    if allowed:
        _insert(database, row)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(database, row)


def test_root_folder_nondurable_identity_and_large_positive_charge_remain_valid(
    database: sqlite3.Connection,
) -> None:
    """空根 directory と memory の False は合法で、占用は 32 bit size に縮めない。"""

    _insert(database, _row(folder="", storage_is_durable=False, size=2**40))
    assert database.execute(
        "SELECT folder, storage_is_durable, size FROM document_upload_intents"
    ).fetchone() == ("", 0, 2**40)


@pytest.mark.parametrize("columns", [
    ("organization_id", "project_id", "actor_id", "upload_key"),
    ("document_id",), ("storage_namespace_id", "storage_key"), ("project_id", "folder", "name"),
])
def test_duplicate_request_document_object_or_pending_path_is_rejected(
    database: sqlite3.Connection, columns: tuple[str, ...],
) -> None:
    """同時 writer の勝者を DB 唯一制約で一つにし、別 object でも同じ PENDING 名は占有する。"""

    original = _row()
    _insert(database, original)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _insert(database, _row(2, **{column: original[column] for column in columns}))


@pytest.mark.parametrize("scope", ["organization_id", "project_id", "actor_id"])
def test_same_upload_key_has_explicit_organization_project_actor_scope(
    database: sqlite3.Connection, scope: str,
) -> None:
    """原鍵の scope を単一 UUID 全域へ広げず、正しい FK を持つ別 scope は別意図にする。"""

    original = _row()
    _insert(database, original)
    other = {"organization_id": 102, "project_id": 202, "actor_id": 302}[scope]
    _insert(database, _row(2, upload_key=original["upload_key"], **{scope: str(UUID(int=other))}))


def test_published_intent_releases_only_pending_name_not_request_object_or_charge(
    database: sqlite3.Connection,
) -> None:
    """公開は path 予約を離すが元鍵・object identity・占用は残り、二重課金解放列を持たない。"""

    original = _row(state="PUBLISHED", published_at=_TIME, cleanup_requested_at=_AFTER)
    _insert(database, original)
    _insert(database, _row(2, name=original["name"]))
    assert database.execute("SELECT sum(size) FROM document_upload_intents").fetchone() == (14,)
    for columns in (("upload_key",), ("document_id",), ("storage_namespace_id", "storage_key")):
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert(database, _row(3, **{column: original[column] for column in columns}))


@pytest.mark.parametrize("state", ["PENDING", "PUBLISHED"])
@pytest.mark.parametrize("parent", ["organizations", "projects", "users"])
def test_retained_intent_restricts_parent_deletion_in_every_state(
    database: sqlite3.Connection, state: str, parent: str,
) -> None:
    """元 actor/Project/Organization を削除して、未決または公開済の帰属を失わせない。"""

    _insert(database, _row(state=state, published_at=_TIME if state == "PUBLISHED" else None))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        database.execute(f"DELETE FROM {parent}")


@pytest.mark.parametrize("column", ["organization_id", "project_id", "actor_id"])
def test_intent_rejects_unknown_restrict_parent(database: sqlite3.Connection, column: str) -> None:
    """欠落親を保存してから後で関連を補う方式を許可しない。"""

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert(database, _row(**{column: str(UUID(int=999))}))


def _document(row: dict[str, object], **changes: object) -> dict[str, object]:
    """同じ元意図に由来する文書 metadata だけを作り、旧行は明示して NULL にする。"""

    document = {column.name: row[column.name] for column in _DOCUMENTS.c if column.name in row}
    document.update(id=row["document_id"], upload_intent_id=row["id"], uploaded_by=row["actor_id"])
    document.update(changes)
    return document


def _insert_document(connection: sqlite3.Connection, document: dict[str, object]) -> None:
    """既存 metadata 全列を実表に挿入し、複合 FK の NULL と exact identity を観察する。"""

    columns = ", ".join(document)
    values = ", ".join(f":{column}" for column in document)
    connection.execute(f"INSERT INTO project_documents ({columns}) VALUES ({values})", document)


@pytest.mark.parametrize("column", ["upload_intent_id", "id", "project_id"])
def test_document_cannot_bind_intent_of_another_document_or_project(
    database: sqlite3.Connection, column: str,
) -> None:
    """intent ID 単体の存在で済ませず、document ID と Project の三つ組を一緒に固定する。"""

    row = _row(state="PUBLISHED", published_at=_TIME)
    _insert(database, row)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_document(database, _document(row, **{column: str(UUID(int=999))}))


def test_document_deletion_preserves_original_receipt_charge_and_restricts_intent_removal(
    database: sqlite3.Connection,
) -> None:
    """metadata 削除は intent の監査/占用を連鎖削除せず、関連中の親意図削除は拒否する。"""

    row = _row(state="PUBLISHED", published_at=_TIME)
    _insert(database, row)
    _insert_document(database, _document(row))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        database.execute("DELETE FROM document_upload_intents")
    database.execute("DELETE FROM project_documents")
    assert database.execute(
        "SELECT id, document_id, state, size FROM document_upload_intents"
    ).fetchone() == (row["id"], row["document_id"], "PUBLISHED", row["size"])


def test_legacy_document_remains_unbound_without_fabricated_intent(
    database: sqlite3.Connection,
) -> None:
    """新 FK は NULL の旧行を引き続き表現し、現在の namespace や原要求を補造しない。"""

    row = _row()
    _insert_document(database, _document(
        row, upload_intent_id=None, storage_namespace_id=None, storage_descriptor_checksum=None,
        storage_is_durable=None,
    ))
    assert database.execute(
        "SELECT id, storage_key, checksum, upload_intent_id FROM project_documents"
    ).fetchone() == (row["document_id"], row["storage_key"], row["checksum"], None)
    assert database.execute("SELECT count(*) FROM document_upload_intents").fetchone() == (0,)


def test_upgrade_only_adds_nullable_binding_to_old_document_rows(migration: ModuleType) -> None:
    """実 add_column を旧表に適用し、既存 ID/key/hash を変えず原要求を未関連で残す。"""

    migration.upgrade()
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE project_documents (id TEXT, storage_key TEXT, checksum TEXT)"
        )
        database.execute("INSERT INTO project_documents VALUES ('old-id', 'old-key', 'old-hash')")
        table, column = migration.op.add_column.call_args.args
        ddl = str(CreateColumn(column).compile(dialect=sqlite.dialect()))
        database.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        assert database.execute("SELECT * FROM project_documents").fetchall() == [
            ("old-id", "old-key", "old-hash", None),
        ]
    migration.op.execute.assert_not_called()


@pytest.mark.parametrize("intent_state", [None, "PENDING", "PUBLISHED", "DAMAGED"])
@pytest.mark.parametrize("binding", [None, str(UUID(int=1)), str(UUID(int=0))])
def test_downgrade_guard_rejects_any_intent_or_document_binding_before_drop(
    migration: ModuleType, intent_state: str | None, binding: str | None,
) -> None:
    """破損行や孤立関連も保護し、server guard と両表 lock が監査破棄より先に動く。"""

    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE document_upload_intents (state TEXT)")
        database.execute("CREATE TABLE project_documents (upload_intent_id TEXT)")
        if intent_state is not None:
            database.execute("INSERT INTO document_upload_intents VALUES (?)", (intent_state,))
        database.execute("INSERT INTO project_documents VALUES (?)", (binding,))

        def execute(statement: str) -> None:
            """server DO の実述語を SQLite で評価し、lock 自体は呼出順だけを検証する。"""

            if statement == _LOCK:
                return
            match = re.fullmatch(
                r"DO \$\$ BEGIN IF (.*?) THEN RAISE EXCEPTION "
                r"'Document upload intents and bindings must be preserved before downgrade'; "
                r"END IF; END \$\$;", statement,
            )
            assert match is not None, "Unexpected SQL or history rewrite"
            result = database.execute("SELECT " + match.group(1)).fetchone()
            assert result is not None
            if result[0]:
                raise RuntimeError("preserve upload intent")

        migration.op.execute.side_effect = execute
        if intent_state is not None or binding is not None:
            with pytest.raises(RuntimeError, match="preserve upload intent"):
                migration.downgrade()
            assert [entry[0] for entry in migration.op.method_calls] == ["execute", "execute"]
        else:
            migration.downgrade()
            assert [entry[0] for entry in migration.op.method_calls] == [
                "execute", "execute", "drop_constraint", "drop_column", "drop_index",
                "drop_index", "drop_table",
            ]
        assert migration.op.method_calls[0] == call.execute(_LOCK)
        assert database.execute("SELECT * FROM project_documents").fetchall() == [(binding,)]
        assert database.execute("SELECT * FROM document_upload_intents").fetchall() == (
            [] if intent_state is None else [(intent_state,)]
        )


@pytest.mark.parametrize("failure_at", [0, 1])
def test_downgrade_lock_or_guard_failure_never_drops_audit(
    migration: ModuleType, failure_at: int,
) -> None:
    """lock や照合失敗を空表とみなさず、どの保護制約も先に外さない。"""

    migration.op.execute.side_effect = [None] * failure_at + [RuntimeError("guard unavailable")]
    with pytest.raises(RuntimeError, match="guard unavailable"):
        migration.downgrade()
    for name in ("drop_constraint", "drop_column", "drop_index", "drop_table"):
        getattr(migration.op, name).assert_not_called()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_postgresql_offline_sql_retains_exact_checks_fk_and_server_guard(
    migration: ModuleType, monkeypatch: pytest.MonkeyPatch, direction: str,
) -> None:
    """offline SQL でも保留判定を省かず、backfill や監査 DELETE を出力しない。"""

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
        assert "CREATE TABLE document_upload_intents" in sql
        for check in _INTENTS.constraints:
            if (
                isinstance(check, sa.CheckConstraint)
                and check.name != "ck_document_upload_intents_publication_closure"
            ):
                assert f"CONSTRAINT {check.name} CHECK ({check.sqltext})" in sql
        assert "publication_closed_at" not in sql
        assert "CREATE UNIQUE INDEX uq_document_upload_intent_pending_path" in sql
        assert (
            "CREATE INDEX ix_document_upload_intents_project_id ON "
            "document_upload_intents (project_id)"
        ) in sql
        assert "(project_id, folder, name) WHERE state = 'PENDING'" in sql
        assert "ADD COLUMN upload_intent_id UUID;" in sql
        assert (
            "FOREIGN KEY(upload_intent_id, id, project_id) REFERENCES "
            "document_upload_intents (id, document_id, project_id) ON DELETE RESTRICT"
        ) in sql
        assert "DROP" not in sql
    else:
        assert _LOCK + ";" in sql
        assert "EXISTS (SELECT 1 FROM document_upload_intents)" in sql
        assert "EXISTS (SELECT 1 FROM project_documents WHERE upload_intent_id IS NOT NULL)" in sql
        assert sql.index(_LOCK) < sql.index("RAISE EXCEPTION") < sql.index("DROP CONSTRAINT")
        assert sql.index("DROP CONSTRAINT") < sql.index("DROP COLUMN") < sql.index("DROP TABLE")
