"""Project の空目录を永続化する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_document_management"
down_revision = "0052_mcp_desktop_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存の文書・blob・凍結 snapshot を変更せず目录を追加する。"""
    op.create_table(
        "project_document_folders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(200), nullable=False),
        sa.UniqueConstraint("project_id", "path", name="uq_document_folders_path"),
    )
    op.create_index(
        "ix_project_document_folders_project_id", "project_document_folders", ["project_id"]
    )


def downgrade() -> None:
    """保存した空目录を無断で失わないよう、非空 table の降級を拒否する。"""
    op.execute("LOCK TABLE project_document_folders IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM project_document_folders) THEN "
        "RAISE EXCEPTION 'Folders prevent downgrade'; END IF; END $$"
    )
    op.drop_table("project_document_folders")
