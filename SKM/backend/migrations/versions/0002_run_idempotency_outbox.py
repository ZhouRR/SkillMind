"""Run idempotency を三元 key へ修正し、transactional Outbox を追加する。

Revision ID: 0002_run_idempotency_outbox
Revises: 0001_m0_run_spine
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_run_idempotency_outbox"
down_revision: str | None = "0001_m0_run_spine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Run request fingerprint と非同期配送用 Outbox table を追加する。"""

    op.add_column(
        "runs",
        sa.Column("trigger_type", sa.String(length=32), server_default="immediate", nullable=False),
    )
    op.add_column("runs", sa.Column("request_hash", sa.String(length=64), nullable=True))
    # 既存 Run は replay 対象にしない識別子を設定し、新規 request hash と衝突させない。
    op.execute(sa.text("UPDATE runs SET request_hash = 'legacy:' || id::text"))
    op.alter_column("runs", "request_hash", nullable=False)
    op.alter_column("runs", "trigger_type", server_default=None)

    op.drop_constraint("uq_runs_project_id", "runs", type_="unique")
    op.create_unique_constraint(
        "uq_runs_project_task_idempotency",
        "runs",
        ["project_id", "task_id", "idempotency_key"],
    )

    # Business transaction と dispatch intent を同時 commit し、Queue 障害時も配送を回復可能にする。
    op.create_table(
        "outbox_messages",
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
    )
    op.create_index(
        "ix_outbox_messages_aggregate_id",
        "outbox_messages",
        ["aggregate_id"],
        unique=False,
    )
    op.create_index(
        "ix_outbox_messages_aggregate_type",
        "outbox_messages",
        ["aggregate_type"],
        unique=False,
    )
    op.create_index("ix_outbox_messages_topic", "outbox_messages", ["topic"], unique=False)
    op.create_index(
        "ix_outbox_messages_pending",
        "outbox_messages",
        ["occurred_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    """Outbox を削除し、旧 Run idempotency constraint へ戻す。"""

    op.drop_index("ix_outbox_messages_pending", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_topic", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_aggregate_type", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_aggregate_id", table_name="outbox_messages")
    op.drop_table("outbox_messages")

    op.drop_constraint("uq_runs_project_task_idempotency", "runs", type_="unique")
    op.create_unique_constraint("uq_runs_project_id", "runs", ["project_id", "idempotency_key"])
    op.drop_column("runs", "request_hash")
    op.drop_column("runs", "trigger_type")
