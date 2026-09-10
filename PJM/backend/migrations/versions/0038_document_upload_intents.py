"""原アップロード意図・占用と文書の正確な関連を保持し、旧行の履歴は補造しない。

Revision ID: 0038_document_upload_intents
Revises: 0037_document_storage_namespace
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_document_upload_intents"
down_revision: str | None = "0037_document_storage_namespace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """PUT 前の保存器だけを追加し、未関連の旧文書へ意図や現接続先を推測で付けない。"""

    op.create_table(
        "document_upload_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("upload_key", sa.Uuid(), nullable=False),
        sa.Column("original_request_id", sa.Uuid(), nullable=False),
        sa.Column("original_session_id", sa.Uuid(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("request_checksum", sa.String(71), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("folder", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("storage_namespace_id", sa.Uuid(), nullable=False),
        sa.Column("storage_descriptor_checksum", sa.String(71), nullable=False),
        sa.Column("storage_is_durable", sa.Boolean(), nullable=False),
        sa.Column("write_protocol", sa.String(32), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("checksum", sa.String(71), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "organization_id", "project_id", "actor_id", "upload_key",
            name="uq_document_upload_intent_request",
        ),
        sa.UniqueConstraint("document_id", name="uq_document_upload_intent_document"),
        sa.UniqueConstraint(
            "storage_namespace_id", "storage_key", name="uq_document_upload_intent_object",
        ),
        sa.UniqueConstraint(
            "id", "document_id", "project_id", name="uq_document_upload_intent_binding",
        ),
        sa.CheckConstraint(
            "upload_key <> '00000000-0000-0000-0000-000000000000' AND "
            "original_request_id <> '00000000-0000-0000-0000-000000000000' AND "
            "original_session_id <> '00000000-0000-0000-0000-000000000000' AND "
            "document_id <> '00000000-0000-0000-0000-000000000000' AND "
            "storage_namespace_id <> '00000000-0000-0000-0000-000000000000'",
            name="non_nil_identities",
        ),
        sa.CheckConstraint("protocol_version = 1", name="protocol_version"),
        sa.CheckConstraint("request_checksum ~ '^sha256:[0-9a-f]{64}$'", name="request_checksum"),
        sa.CheckConstraint(
            "storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name="storage_descriptor_checksum",
        ),
        sa.CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
        sa.CheckConstraint(
            "name <> '' AND storage_key <> '' AND mime <> ''", name="nonempty_fields",
        ),
        sa.CheckConstraint("write_protocol = 'UNCONDITIONAL_V1'", name="write_protocol"),
        sa.CheckConstraint("size > 0", name="positive_size"),
        sa.CheckConstraint("state IN ('PENDING', 'PUBLISHED')", name="state"),
        sa.CheckConstraint(
            "(state = 'PENDING' AND published_at IS NULL AND cleanup_requested_at IS NULL) OR "
            "(state = 'PUBLISHED' AND published_at IS NOT NULL AND published_at >= created_at "
            "AND (cleanup_requested_at IS NULL OR cleanup_requested_at >= published_at))",
            name="publication",
        ),
    )
    op.create_index(
        "uq_document_upload_intent_pending_path", "document_upload_intents",
        ["project_id", "folder", "name"], unique=True,
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.create_index(
        "ix_document_upload_intents_project_id", "document_upload_intents", ["project_id"],
    )
    op.add_column("project_documents", sa.Column("upload_intent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_project_documents_upload_intent", "project_documents", "document_upload_intents",
        ["upload_intent_id", "id", "project_id"], ["id", "document_id", "project_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """占用・公開回执を失う回退は拒否し、両表 lock 後に完全未使用だけを破棄する。"""

    # offline SQL も server 判定を含め、意図/関連の検査後から DROP まで新規保存を止める。
    op.execute("LOCK TABLE project_documents, document_upload_intents IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_upload_intents) OR "
        "EXISTS (SELECT 1 FROM project_documents WHERE upload_intent_id IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Document upload intents and bindings must be preserved "
        "before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint(
        "fk_project_documents_upload_intent", "project_documents", type_="foreignkey",
    )
    op.drop_column("project_documents", "upload_intent_id")
    op.drop_index("ix_document_upload_intents_project_id", table_name="document_upload_intents")
    op.drop_index("uq_document_upload_intent_pending_path", table_name="document_upload_intents")
    op.drop_table("document_upload_intents")
