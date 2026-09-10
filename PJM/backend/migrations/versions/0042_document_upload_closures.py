"""原 PENDING の公開停止を保持し、旧入力 hash・公開回执・占用は変更しない。

Revision ID: 0042_document_upload_closures
Revises: 0041_evaluation_submissions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_document_upload_closures"
down_revision: str | None = "0041_evaluation_submissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """旧行は未閉鎖のまま保持し、新しい停止事実だけを専用監査に保存する。"""

    op.add_column(
        "document_upload_intents",
        sa.Column("publication_closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "publication_closure", "document_upload_intents",
        "publication_closed_at IS NULL OR (state = 'PENDING' AND published_at IS NULL "
        "AND publication_closed_at >= created_at)",
    )
    op.drop_index("uq_document_upload_intent_pending_path", table_name="document_upload_intents")
    op.create_index(
        "uq_document_upload_intent_pending_path", "document_upload_intents",
        ["project_id", "folder", "name"], unique=True,
        postgresql_where=sa.text("state = 'PENDING' AND publication_closed_at IS NULL"),
    )
    op.create_table(
        "document_upload_closures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("upload_intent_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("upload_key", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("binding_checksum", sa.String(71), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["upload_intent_id", "document_id", "project_id"],
            [
                "document_upload_intents.id", "document_upload_intents.document_id",
                "document_upload_intents.project_id",
            ],
            name="fk_document_upload_closures_upload_intent", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("upload_intent_id", name="uq_document_upload_closure_intent"),
        sa.CheckConstraint(
            "id <> '00000000-0000-0000-0000-000000000000' AND "
            "upload_intent_id <> '00000000-0000-0000-0000-000000000000' AND "
            "organization_id <> '00000000-0000-0000-0000-000000000000' AND "
            "project_id <> '00000000-0000-0000-0000-000000000000' AND "
            "actor_id <> '00000000-0000-0000-0000-000000000000' AND "
            "upload_key <> '00000000-0000-0000-0000-000000000000' AND "
            "document_id <> '00000000-0000-0000-0000-000000000000' AND "
            "requested_by <> '00000000-0000-0000-0000-000000000000' AND "
            "request_id <> '00000000-0000-0000-0000-000000000000' AND "
            "session_id <> '00000000-0000-0000-0000-000000000000'",
            name="non_nil_identities",
        ),
        sa.CheckConstraint("protocol_version = 1", name="protocol_version"),
        sa.CheckConstraint("actor_id = requested_by", name="original_actor"),
        sa.CheckConstraint("binding_checksum ~ '^sha256:[0-9a-f]{64}$'", name="binding_checksum"),
    )
    op.create_index(
        "ix_document_upload_closures_project_id", "document_upload_closures", ["project_id"],
    )


def downgrade() -> None:
    """停止事実を失う回退は offline SQL でも拒否し、監査を消して続行しない。"""

    op.execute(
        "LOCK TABLE document_upload_intents, document_upload_closures IN ACCESS EXCLUSIVE MODE",
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_upload_closures) OR "
        "EXISTS (SELECT 1 FROM document_upload_intents WHERE publication_closed_at IS NOT NULL) "
        "THEN RAISE EXCEPTION 'Document upload closures must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_index("ix_document_upload_closures_project_id", table_name="document_upload_closures")
    op.drop_table("document_upload_closures")
    op.drop_index("uq_document_upload_intent_pending_path", table_name="document_upload_intents")
    op.create_index(
        "uq_document_upload_intent_pending_path", "document_upload_intents",
        ["project_id", "folder", "name"], unique=True,
        postgresql_where=sa.text("state = 'PENDING'"),
    )
    op.drop_constraint("publication_closure", "document_upload_intents", type_="check")
    op.drop_column("document_upload_intents", "publication_closed_at")
