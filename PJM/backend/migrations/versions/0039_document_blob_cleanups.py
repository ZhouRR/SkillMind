"""文書削除の原対象を独立保存し、旧目録の占用を清理未確認のまま失わせない。

Revision ID: 0039_document_blob_cleanups
Revises: 0038_document_upload_intents
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_document_blob_cleanups"
down_revision: str | None = "0038_document_upload_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新しい削除の保存器だけを作り、既存文書に架空の要求や namespace を補わない。"""

    op.create_table(
        "document_blob_cleanups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("upload_intent_id", sa.Uuid(), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("source_protocol", sa.String(32), nullable=False),
        sa.Column("folder", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("checksum", sa.String(71), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("document_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("storage_namespace_id", sa.Uuid(), nullable=False),
        sa.Column("storage_descriptor_checksum", sa.String(71), nullable=False),
        sa.Column("storage_is_durable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["upload_intent_id", "document_id", "project_id"],
            [
                "document_upload_intents.id", "document_upload_intents.document_id",
                "document_upload_intents.project_id",
            ],
            name="fk_document_blob_cleanups_upload_intent", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("document_id", name="uq_document_blob_cleanup_document"),
        sa.CheckConstraint(
            "document_id <> '00000000-0000-0000-0000-000000000000' AND "
            "request_id <> '00000000-0000-0000-0000-000000000000' AND "
            "session_id <> '00000000-0000-0000-0000-000000000000' AND "
            "uploaded_by <> '00000000-0000-0000-0000-000000000000' AND "
            "storage_namespace_id <> '00000000-0000-0000-0000-000000000000'",
            name="non_nil_identities",
        ),
        sa.CheckConstraint("protocol_version = 1", name="protocol_version"),
        sa.CheckConstraint(
            "(upload_intent_id IS NOT NULL AND source_protocol = 'UPLOAD_INTENT_V1') OR "
            "(upload_intent_id IS NULL AND source_protocol = 'LEGACY_UNVERIFIED')",
            name="source_protocol",
        ),
        sa.CheckConstraint(
            "name <> '' AND storage_key <> '' AND mime <> ''", name="nonempty_fields",
        ),
        sa.CheckConstraint("size > 0", name="positive_size"),
        sa.CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
        sa.CheckConstraint(
            "storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name="storage_descriptor_checksum",
        ),
        sa.CheckConstraint("created_at >= document_created_at", name="created_at_order"),
    )
    op.create_index(
        "ix_document_blob_cleanups_project_id", "document_blob_cleanups", ["project_id"],
    )


def downgrade() -> None:
    """清理要求は完了推定で消さず、排他 lock 後に未使用表だけを破棄する。"""

    op.execute("LOCK TABLE document_blob_cleanups IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_blob_cleanups) THEN "
        "RAISE EXCEPTION 'Document blob cleanup records must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_index("ix_document_blob_cleanups_project_id", table_name="document_blob_cleanups")
    op.drop_table("document_blob_cleanups")
