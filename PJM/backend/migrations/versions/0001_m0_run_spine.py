"""M0 の Run、RunAttempt、RunEvent の基幹 table を作成する。

Revision ID: 0001_m0_run_spine
Revises:
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_m0_run_spine"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """監査可能な Run spine を構成する table と index を追加する。"""

    # Run は入力と権限の snapshot を保持し、再試行時にも上書きしない。
    op.create_table(
        "runs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("task_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("permission_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("selected_sources_json", sa.JSON(), nullable=False),
        sa.Column("limits_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("project_id", "idempotency_key", name=op.f("uq_runs_project_id")),
    )
    op.create_index(op.f("ix_runs_project_id"), "runs", ["project_id"], unique=False)
    op.create_index(op.f("ix_runs_task_id"), "runs", ["task_id"], unique=False)

    # 実行試行を分離し、lease 失効や復旧の履歴を追加形式で残す。
    op.create_table(
        "run_attempts",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_attempts_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_attempts")),
        sa.UniqueConstraint("run_id", "attempt_no", name=op.f("uq_run_attempts_run_id")),
    )
    op.create_index(op.f("ix_run_attempts_run_id"), "run_attempts", ["run_id"], unique=False)

    # SSE replay の順序を保証するため、Run 内 sequence に一意制約を設定する。
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_run_events_run_attempt_id_run_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_events_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_events")),
        sa.UniqueConstraint("run_id", "sequence", name=op.f("uq_run_events_run_id")),
    )
    op.create_index(
        op.f("ix_run_events_run_attempt_id"),
        "run_events",
        ["run_attempt_id"],
        unique=False,
    )
    op.create_index(op.f("ix_run_events_run_id"), "run_events", ["run_id"], unique=False)


def downgrade() -> None:
    """依存関係の逆順で M0 Run spine を削除する。"""

    op.drop_index(op.f("ix_run_events_run_id"), table_name="run_events")
    op.drop_index(op.f("ix_run_events_run_attempt_id"), table_name="run_events")
    op.drop_table("run_events")
    op.drop_index(op.f("ix_run_attempts_run_id"), table_name="run_attempts")
    op.drop_table("run_attempts")
    op.drop_index(op.f("ix_runs_task_id"), table_name="runs")
    op.drop_index(op.f("ix_runs_project_id"), table_name="runs")
    op.drop_table("runs")
