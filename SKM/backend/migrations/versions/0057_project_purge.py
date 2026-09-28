"""Project 本体の寿命から監査・確定 upload・blob 清理の保持を分離する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_project_purge"
down_revision = "0056_inline_effect_owner"
branch_labels = None
depends_on = None

# 複合 upload FK は保持する。Project/member の削除だけを監査から独立させる。
DETACHED_REFERENCES = (
    ("project_member_events", "project_id", "projects"),
    ("project_member_events", "member_id", "project_members"),
    ("run_deletion_audits", "project_id", "projects"),
    ("document_upload_intents", "project_id", "projects"),
    ("document_upload_closures", "project_id", "projects"),
    ("document_blob_cleanups", "project_id", "projects"),
)


def upgrade() -> None:
    """既存監査の値を変更せず、組織に帰属する Project 削除監査を追加する。"""
    op.create_table(
        "project_deletion_audits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("project_key", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_project_deletion_audits_organization_id", "project_deletion_audits", ["organization_id"]
    )
    for table, column, parent in DETACHED_REFERENCES:
        op.drop_constraint(f"fk_{table}_{column}_{parent}", table, type_="foreignkey")


def downgrade() -> None:
    """完全削除済みの監査を消して FK を復活させる回退は禁止する。"""
    op.execute("LOCK TABLE project_deletion_audits, projects IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM project_deletion_audits) THEN "
        "RAISE EXCEPTION 'Project deletion audit prevents downgrade'; END IF; END $$"
    )
    for table, column, parent in reversed(DETACHED_REFERENCES):
        op.create_foreign_key(
            f"fk_{table}_{column}_{parent}", table, parent, [column], ["id"], ondelete="RESTRICT"
        )
    op.drop_table("project_deletion_audits")
