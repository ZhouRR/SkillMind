"""原予定の認領と結算を保存し、旧摘要から発火履歴や正確な計数を補造しない。

Revision ID: 0036_schedule_occurrences
Revises: 0035_budget_invocations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_schedule_occurrences"
down_revision: str | None = "0035_budget_invocations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """旧 Schedule は protocol 0 のまま残し、新方式を選んだ新規行だけが発火できる。"""

    op.add_column(
        "task_schedules",
        sa.Column(
            "configuration_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.add_column(
        "task_schedules",
        sa.Column("occurrence_protocol", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "configuration_version", "task_schedules", "configuration_version >= 1"
    )
    op.create_check_constraint(
        "occurrence_protocol", "task_schedules", "occurrence_protocol IN (0, 1)"
    )
    op.create_table(
        "task_schedule_occurrences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("occurrence_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("configuration_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("lease_token_hash", sa.String(64), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("detail", sa.String(512), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["schedule_id"], ["task_schedules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "schedule_id", "occurrence_at", name="uq_task_schedule_occurrence_identity"
        ),
        sa.UniqueConstraint("run_id", name="uq_task_schedule_occurrence_run"),
        sa.CheckConstraint("configuration_version >= 1", name="configuration_version"),
        sa.CheckConstraint("lease_generation >= 1", name="lease_generation"),
        sa.CheckConstraint("attempt_count >= 1", name="attempt_count"),
        sa.CheckConstraint("status IN ('PENDING', 'SETTLED')", name="status"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('RUN_CREATED', 'SKIPPED_OVERLAP', 'FAILED_PRECONDITION')",
            name="outcome",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND run_id IS NULL AND outcome IS NULL AND settled_at IS NULL) OR "
            "(status = 'SETTLED' AND outcome IS NOT NULL AND settled_at IS NOT NULL AND "
            "((outcome = 'RUN_CREATED' AND run_id IS NOT NULL) OR "
            "(outcome IN ('SKIPPED_OVERLAP', 'FAILED_PRECONDITION') AND run_id IS NULL)))",
            name="settlement",
        ),
    )
    op.create_index(
        "uq_task_schedule_occurrence_pending",
        "task_schedule_occurrences",
        ["schedule_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    """認領・結算監査と使用済み protocol を保持し、lock 後の判定だけで削除を許可する。"""

    op.execute("LOCK TABLE task_schedules, task_schedule_occurrences IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM task_schedule_occurrences) OR "
        "EXISTS (SELECT 1 FROM task_schedules WHERE occurrence_protocol = 1 "
        "OR configuration_version <> 1) THEN "
        "RAISE EXCEPTION 'Schedule occurrences and enabled protocols must be preserved "
        "before downgrade'; END IF; END $$;"
    )
    op.drop_index("uq_task_schedule_occurrence_pending", table_name="task_schedule_occurrences")
    op.drop_table("task_schedule_occurrences")
    op.drop_constraint(
        op.f("ck_task_schedules_occurrence_protocol"), "task_schedules", type_="check"
    )
    op.drop_constraint(
        op.f("ck_task_schedules_configuration_version"), "task_schedules", type_="check"
    )
    op.drop_column("task_schedules", "occurrence_protocol")
    op.drop_column("task_schedules", "configuration_version")
