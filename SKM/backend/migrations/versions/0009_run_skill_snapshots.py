"""Run と公開 Skill version の凍結された対応関係を追加する。

Revision ID: 0009_run_skill_snapshots
Revises: 0008_skill_versions
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_run_skill_snapshots"
down_revision: str | None = "0008_skill_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Run/version binding table と参照・順序の制約を作成する。"""

    op.create_table(
        "run_skill_snapshots",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("manifest_checksum", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_skill_snapshots")),
        sa.UniqueConstraint("run_id", "sort_order", name="uq_run_skill_snapshots_run_order"),
        sa.UniqueConstraint(
            "run_id", "skill_version_id", name="uq_run_skill_snapshots_run_version"
        ),
    )
    op.create_index(op.f("ix_run_skill_snapshots_run_id"), "run_skill_snapshots", ["run_id"])
    op.create_index(
        op.f("ix_run_skill_snapshots_skill_version_id"),
        "run_skill_snapshots",
        ["skill_version_id"],
    )


def downgrade() -> None:
    """Run/version binding table を削除する。"""

    op.drop_table("run_skill_snapshots")
