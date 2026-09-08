"""Run input の準備回执を導入する。旧 workspace を読み取って補签しない。

Revision ID: 0029_run_input_snapshots
Revises: 0028_skill_source_file_index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_run_input_snapshots"
down_revision: str | None = "0028_skill_source_file_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Run 唯一の認領世代を保持し、完成回执は現在 lease の Worker だけが書く。"""

    op.create_table(
        "run_input_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "prepared_by_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("run_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_checksum", sa.String(71), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("files_json", sa.JSON(), nullable=False),
        sa.Column("tree_checksum", sa.String(71), nullable=True),
        sa.Column("total_files", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", name="uq_run_input_snapshots_run"),
        sa.CheckConstraint("status IN ('PREPARING', 'READY')", name="ck_run_input_snapshot_status"),
        sa.CheckConstraint(
            "total_files >= 0 AND total_bytes >= 0", name="ck_run_input_snapshot_totals"
        ),
        sa.CheckConstraint(
            "(status = 'PREPARING' AND completed_at IS NULL AND tree_checksum IS NULL) OR "
            "(status = 'READY' AND completed_at IS NOT NULL AND tree_checksum IS NOT NULL)",
            name="ck_run_input_snapshot_completion",
        ),
    )


def downgrade() -> None:
    """可信回执の黙示消失を拒否する。保持/移送を確認してから回退計画を適用する。"""

    op.execute(
        sa.text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM run_input_snapshots) THEN
                RAISE EXCEPTION 'Run input receipts must be preserved before downgrade';
            END IF;
        END $$;
    """)
    )
    op.drop_table("run_input_snapshots")
