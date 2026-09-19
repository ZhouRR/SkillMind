"""目录/回収箱 migration の生成 DDL と現行 metadata の関係を検証する。"""

from __future__ import annotations

from io import StringIO

from alembic.migration import MigrationContext
from alembic.operations import Operations

from skillmind.db.models import ProjectDocument, ProjectDocumentEffectUpload, RunDeletionAudit
from tests.db.test_document_storage_namespace_migration import _migration


def migration_sql(name: str, direction: str = "upgrade") -> str:
    """接続なしで migration の実 entry point を PostgreSQL SQL に変換する。"""
    buffer = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    module = _migration(name)
    with Operations.context(context):
        getattr(module, direction)()
    return buffer.getvalue()


def test_partial_path_indexes_match_recycle_semantics():
    """現存文書だけが表示 path を占有し、公開済み回执は原 path を保持できる。"""
    sql = migration_sql("0054_history_recycle_bin.py")
    assert "WHERE deleted_at IS NULL" in sql
    assert "WHERE state <> 'PUBLISHED'" in sql
    assert "deleted_by_run_id" in sql
    for model, name, predicate in (
        (ProjectDocument, "uq_project_documents_project_folder_name", "deleted_at IS NULL"),
        (ProjectDocumentEffectUpload, "uq_document_effect_upload_path", "state <> 'PUBLISHED'"),
    ):
        index = next(i for i in model.__table__.indexes if i.name == name)
        assert index.unique and str(index.dialect_options["postgresql"]["where"]) == predicate


def test_deletion_audit_does_not_keep_run_or_skill_foreign_keys():
    """削除記録は版の解放を阻害せず、元 request key の再実行を防ぐ。"""
    sql = migration_sql("0055_run_deletion_audit.py")
    assert "idempotency_key" in sql and "uq_run_deletion_key" in sql
    assert {fk.column.table.name for fk in RunDeletionAudit.__table__.foreign_keys} == {"projects"}
    for name in (
        "0053_document_management.py",
        "0054_history_recycle_bin.py",
        "0055_run_deletion_audit.py",
    ):
        downgrade = migration_sql(name, "downgrade")
        assert downgrade.index("LOCK TABLE") < downgrade.index("IF EXISTS")
        assert "RAISE EXCEPTION" in downgrade
