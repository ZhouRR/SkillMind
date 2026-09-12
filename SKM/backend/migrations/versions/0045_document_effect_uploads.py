"""成果の原 Effect・占用・送信/公開回执を文書と別に保持する。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_document_effect_uploads"
down_revision: str | None = "0044_interpretation_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空の成果台帳と任意 origin 列を追加し、既存文書の履歴は変更しない。"""
    op.create_table(
        "document_effect_uploads",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("effect_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_ref", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("request_checksum", sa.String(length=71), nullable=False),
        sa.Column("folder", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("bucket", sa.String(length=63), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("storage_namespace_id", sa.Uuid(), nullable=False),
        sa.Column("storage_descriptor_checksum", sa.String(length=71), nullable=False),
        sa.Column("storage_is_durable", sa.Boolean(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False),
        sa.Column("checksum", sa.String(length=71), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("put_owner_id", sa.Uuid(), nullable=True),
        sa.Column("etag", sa.String(length=258), nullable=True),
        sa.Column("version_id", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(state = 'RESERVED' AND sent_at IS NULL AND verified_at IS NULL AND "
            "published_at IS NULL AND put_owner_id IS NULL) OR (state = 'SENT' AND "
            "sent_at IS NOT NULL AND sent_at >= created_at AND verified_at IS NULL AND"
            " published_at IS NULL AND put_owner_id IS NOT NULL) OR (state = "
            "'VERIFIED' AND sent_at IS NOT NULL AND verified_at IS NOT NULL AND "
            "sent_at >= created_at AND verified_at >= sent_at AND published_at IS NULL"
            " AND put_owner_id IS NOT NULL) OR (state = 'PUBLISHED' AND sent_at IS NOT"
            " NULL AND verified_at IS NOT NULL AND published_at IS NOT NULL AND "
            "sent_at >= created_at AND verified_at >= sent_at AND published_at >= "
            "verified_at AND put_owner_id IS NOT NULL)",
            name=op.f("ck_document_effect_uploads_state_times"),
        ),
        sa.CheckConstraint(
            "(state IN ('RESERVED', 'SENT') AND etag IS NULL AND version_id IS NULL) "
            "OR (state IN ('VERIFIED', 'PUBLISHED') AND etag IS NOT NULL AND etag <> "
            "'')",
            name=op.f("ck_document_effect_uploads_receipt"),
        ),
        sa.CheckConstraint(
            "artifact_ref ~ '^art_[a-zA-Z0-9_-]+$'",
            name=op.f("ck_document_effect_uploads_artifact_ref"),
        ),
        sa.CheckConstraint(
            "checksum ~ '^sha256:[0-9a-f]{64}$'", name=op.f("ck_document_effect_uploads_checksum")
        ),
        sa.CheckConstraint(
            "mime IN ('text/plain', 'text/markdown', 'application/json')",
            name=op.f("ck_document_effect_uploads_mime"),
        ),
        sa.CheckConstraint(
            "name <> '' AND storage_key <> '' AND bucket <> ''",
            name=op.f("ck_document_effect_uploads_nonempty_fields"),
        ),
        sa.CheckConstraint(
            "request_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_document_effect_uploads_request_checksum"),
        ),
        sa.CheckConstraint(
            "storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_document_effect_uploads_storage_descriptor_checksum"),
        ),
        sa.CheckConstraint(
            "protocol_version = 1", name=op.f("ck_document_effect_uploads_protocol_version")
        ),
        sa.CheckConstraint(
            "size > 0 AND size <= 1048576", name=op.f("ck_document_effect_uploads_size")
        ),
        sa.CheckConstraint("storage_is_durable", name=op.f("ck_document_effect_uploads_durable")),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_document_effect_uploads_actor_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["effect_id"],
            ["effect_executions.id"],
            name=op.f("fk_document_effect_uploads_effect_id_effect_executions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_document_effect_uploads_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_document_effect_uploads_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_document_effect_uploads_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_effect_uploads")),
        sa.UniqueConstraint("document_id", name="uq_document_effect_upload_document"),
        sa.UniqueConstraint("effect_id", name="uq_document_effect_upload_effect"),
        sa.UniqueConstraint(
            "id", "document_id", "project_id", name="uq_document_effect_upload_binding"
        ),
        sa.UniqueConstraint("project_id", "folder", "name", name="uq_document_effect_upload_path"),
        sa.UniqueConstraint(
            "storage_namespace_id", "storage_key", name="uq_document_effect_upload_object"
        ),
    )
    op.create_index(
        op.f("ix_document_effect_uploads_project_id"),
        "document_effect_uploads",
        ["project_id"],
        unique=False,
    )
    op.add_column("project_documents", sa.Column("effect_upload_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_project_documents_effect_upload",
        "project_documents",
        "document_effect_uploads",
        ["effect_upload_id", "id", "project_id"],
        ["id", "document_id", "project_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "single_upload_origin",
        "project_documents",
        "upload_intent_id IS NULL OR effect_upload_id IS NULL",
    )


def downgrade() -> None:
    """原要求が一つでもあれば、状態に関係なく削除前に降級を拒否する。"""

    op.execute("LOCK TABLE document_effect_uploads, project_documents IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_effect_uploads) THEN "
        "RAISE EXCEPTION 'Document effect upload records must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint("single_upload_origin", "project_documents", type_="check")
    op.drop_constraint(
        "fk_project_documents_effect_upload", "project_documents", type_="foreignkey"
    )
    op.drop_column("project_documents", "effect_upload_id")
    op.drop_index("ix_document_effect_uploads_project_id", table_name="document_effect_uploads")
    op.drop_table("document_effect_uploads")
