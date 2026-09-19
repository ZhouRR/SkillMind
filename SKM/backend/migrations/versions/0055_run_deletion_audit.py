"""完全削除後に最小の実施監査を保持する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_run_deletion_audit"
down_revision = "0054_history_recycle_bin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """実行・Skill への削除阻害 FK を持たない監査を追加する。"""
    op.create_table(
        "run_deletion_audits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.UniqueConstraint("project_id", "task_id", "idempotency_key", name="uq_run_deletion_key"),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("output_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_deletion_audits_project_id", "run_deletion_audits", ["project_id"])


def downgrade() -> None:
    """保存された削除監査は降級で捨てない。"""
    op.execute("LOCK TABLE run_deletion_audits IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM run_deletion_audits) THEN "
        "RAISE EXCEPTION 'Deletion audit prevents downgrade'; END IF; END $$"
    )
    op.drop_table("run_deletion_audits")
