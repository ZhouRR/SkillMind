"""目录管理の原 Effect 回执と同一 path 更新の元文書参照を保存する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_document_file_mutations"
down_revision = "0057_project_purge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存 byte/metadata を変更せず、原要求の照会可能な記録を追加する。"""
    op.drop_constraint(
        op.f("ck_change_proposals_document_library_integration"), "change_proposals", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_change_proposals_document_library_integration"),
        "change_proposals",
        "(capability_version = 'document.write/v1' AND operation IN ('CREATE', 'UPDATE', "
        "'MOVE', 'MOVE_FOLDER', 'CREATE_FOLDER', 'DELETE_FOLDER', 'TRASH', 'RESTORE') "
        "AND integration_id IS NULL) OR "
        "(capability_version <> 'document.write/v1' AND integration_id IS NOT NULL)",
    )
    op.create_table(
        "document_mutation_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "effect_id",
            sa.Uuid(),
            sa.ForeignKey("effect_executions.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_checksum", sa.String(71), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("run_id", "project_id"):
        op.create_index(
            f"ix_document_mutation_receipts_{column}", "document_mutation_receipts", [column]
        )
    op.add_column(
        "document_effect_uploads", sa.Column("replaces_document_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "document_effect_uploads",
        sa.Column("expected_document_revision", sa.String(71), nullable=True),
    )
    op.add_column(
        "document_effect_uploads",
        sa.Column("publication_closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index("uq_document_effect_upload_path", table_name="document_effect_uploads")
    op.create_index(
        "uq_document_effect_upload_path",
        "document_effect_uploads",
        ["project_id", "folder", "name"],
        unique=True,
        postgresql_where=sa.text("state <> 'PUBLISHED' AND publication_closed_at IS NULL"),
    )


def downgrade() -> None:
    """使用した回执や更新の根拠を破棄する downgrade は拒否する。"""
    op.execute(
        "LOCK TABLE change_proposals, document_mutation_receipts, document_effect_uploads IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_mutation_receipts) OR EXISTS "
        "(SELECT 1 FROM document_effect_uploads WHERE replaces_document_id IS NOT NULL "
        "OR publication_closed_at IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM change_proposals WHERE capability_version = 'document.write/v1' "
        "AND operation <> 'CREATE') THEN RAISE EXCEPTION 'Document mutation history prevents downgrade'; END IF; END $$"
    )
    op.drop_constraint(
        op.f("ck_change_proposals_document_library_integration"), "change_proposals", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_change_proposals_document_library_integration"),
        "change_proposals",
        "(capability_version = 'document.write/v1' AND operation = 'CREATE' "
        "AND integration_id IS NULL) OR "
        "(capability_version <> 'document.write/v1' AND integration_id IS NOT NULL)",
    )
    op.drop_index("uq_document_effect_upload_path", table_name="document_effect_uploads")
    op.create_index(
        "uq_document_effect_upload_path",
        "document_effect_uploads",
        ["project_id", "folder", "name"],
        unique=True,
        postgresql_where=sa.text("state <> 'PUBLISHED'"),
    )
    op.drop_column("document_effect_uploads", "publication_closed_at")
    op.drop_column("document_effect_uploads", "expected_document_revision")
    op.drop_column("document_effect_uploads", "replaces_document_id")
    op.drop_table("document_mutation_receipts")
