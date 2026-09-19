"""履歴と文書の回収箱。監査・原回执・blob の削除は行わない。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_history_recycle_bin"
down_revision = "0053_document_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存履歴を変更せず、回収箱と生存文書の path 一意性を追加する。"""
    op.drop_constraint("uq_document_effect_upload_path", "document_effect_uploads", type_="unique")
    op.create_index(
        "uq_document_effect_upload_path",
        "document_effect_uploads",
        ["project_id", "folder", "name"],
        unique=True,
        postgresql_where=sa.text("state <> 'PUBLISHED'"),
    )
    for table in ("runs", "project_documents"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True)))
        op.add_column(table, sa.Column("deleted_by", sa.Uuid()))
    op.add_column("project_documents", sa.Column("deleted_by_run_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_documents_deleted_by_run",
        "project_documents",
        "runs",
        ["deleted_by_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_project_documents_project_folder_name", "project_documents", type_="unique"
    )
    op.create_index(
        "uq_project_documents_project_folder_name",
        "project_documents",
        ["project_id", "folder", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """回収箱の記録があれば降級しない。"""
    op.execute(
        "LOCK TABLE runs, project_documents, document_effect_uploads IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM runs WHERE deleted_at IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM project_documents WHERE deleted_at IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Recycle bin prevents downgrade'; END IF; END $$"
    )
    op.drop_index("uq_document_effect_upload_path", table_name="document_effect_uploads")
    op.create_unique_constraint(
        "uq_document_effect_upload_path",
        "document_effect_uploads",
        ["project_id", "folder", "name"],
    )
    op.drop_index("uq_project_documents_project_folder_name", table_name="project_documents")
    op.create_unique_constraint(
        "uq_project_documents_project_folder_name",
        "project_documents",
        ["project_id", "folder", "name"],
    )
    op.drop_constraint("fk_documents_deleted_by_run", "project_documents", type_="foreignkey")
    op.drop_column("project_documents", "deleted_by_run_id")
    for table in ("runs", "project_documents"):
        op.drop_column(table, "deleted_by")
        op.drop_column(table, "deleted_at")
