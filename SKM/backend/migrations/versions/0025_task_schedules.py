"""Task の時刻起動を保持する task_schedules table を追加する。

Revision ID: 0025_task_schedules
Revises: 0023_managed_secrets
Create Date: 2026-07-26

docs/01 §22。凍結した SkillVersion・task・入力・資源選択と、発火形態 (ONCE / CRON)、IANA
timezone、次回発火時刻を一行に持つ。`(status, next_run_at)` の index は worker の到期走査用で、
認領は同 column の CAS で行うため専用の lease column は持たない。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_task_schedules"
down_revision: str | None = "0023_managed_secrets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """task_schedules table と到期走査 index を作成する。"""

    op.create_table(
        "task_schedules",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("cron_expression", sa.String(length=128), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(length=200), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("sources_json", sa.JSON(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_outcome", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("missed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('ONCE', 'CRON')", name=op.f("ck_task_schedules_kind")),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED')",
            name=op.f("ck_task_schedules_status"),
        ),
        sa.CheckConstraint(
            "(kind = 'CRON' AND cron_expression IS NOT NULL AND run_at IS NULL)"
            " OR (kind = 'ONCE' AND run_at IS NOT NULL AND cron_expression IS NULL)",
            name=op.f("ck_task_schedules_kind_fields"),
        ),
        sa.CheckConstraint(
            "max_runs IS NULL OR max_runs > 0", name=op.f("ck_task_schedules_max_runs")
        ),
        sa.CheckConstraint("run_count >= 0", name=op.f("ck_task_schedules_run_count")),
        sa.CheckConstraint("missed_count >= 0", name=op.f("ck_task_schedules_missed_count")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_task_schedules_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name=op.f("fk_task_schedules_skill_version_id_skill_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"],
            ["runs.id"],
            name=op.f("fk_task_schedules_last_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_task_schedules_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_schedules")),
    )
    op.create_index(
        op.f("ix_task_schedules_project_id"), "task_schedules", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_task_schedules_skill_version_id"),
        "task_schedules",
        ["skill_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_task_schedules_created_by"), "task_schedules", ["created_by"], unique=False
    )
    # 到期走査は常に「ACTIVE かつ next_run_at <= now」なので複合 index を明示する。
    op.create_index(
        "ix_task_schedules_due", "task_schedules", ["status", "next_run_at"], unique=False
    )


def downgrade() -> None:
    """task_schedules table を削除する。"""

    op.drop_index("ix_task_schedules_due", table_name="task_schedules")
    op.drop_index(op.f("ix_task_schedules_created_by"), table_name="task_schedules")
    op.drop_index(op.f("ix_task_schedules_skill_version_id"), table_name="task_schedules")
    op.drop_index(op.f("ix_task_schedules_project_id"), table_name="task_schedules")
    op.drop_table("task_schedules")
