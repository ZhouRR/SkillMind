"""Run 共通予算の持久載体を導入する。履歴の残高や消費を推測して補填しない。

Revision ID: 0030_run_budget_ledger
Revises: 0029_run_input_snapshots
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_run_budget_ledger"
down_revision: str | None = "0029_run_input_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """勘定・預留・追加回执を作るだけで、既存 Run や Worker を自動移行しない。"""

    op.create_table(
        "run_budget_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("policy_checksum", sa.String(64), nullable=False),
        sa.Column("limits_checksum", sa.String(64), nullable=False),
        sa.Column("consumed_turns", sa.Numeric(38, 0), nullable=False),
        sa.Column("reserved_turns", sa.Numeric(38, 0), nullable=False),
        sa.Column("consumed_cost_nanos", sa.Numeric(38, 0)),
        sa.Column("reserved_cost_nanos", sa.Numeric(38, 0)),
        sa.Column("block_code", sa.String(64)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_run_budget_accounts_run"),
        sa.CheckConstraint(
            "consumed_turns >= 0 AND reserved_turns >= 0", name="budget_account_turns"
        ),
        sa.CheckConstraint(
            "(consumed_cost_nanos IS NULL AND reserved_cost_nanos IS NULL) OR "
            "(consumed_cost_nanos IS NOT NULL AND reserved_cost_nanos IS NOT NULL "
            "AND consumed_cost_nanos >= 0 AND reserved_cost_nanos >= 0)",
            name="budget_account_cost",
        ),
        sa.CheckConstraint("row_version >= 1", name="budget_account_version"),
    )
    op.create_table(
        "run_budget_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("run_budget_accounts.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_segment_id",
            sa.Uuid(),
            sa.ForeignKey("run_segments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_attempt_id",
            sa.Uuid(),
            sa.ForeignKey("run_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("execution_key", sa.String(128), nullable=False),
        sa.Column("group_key", sa.String(128), nullable=False),
        sa.Column("group_checksum", sa.String(64), nullable=False),
        sa.Column("request_checksum", sa.String(64), nullable=False),
        sa.Column(
            "parent_reservation_id",
            sa.Uuid(),
            sa.ForeignKey("run_budget_reservations.id", ondelete="RESTRICT"),
        ),
        sa.Column("execution_lease_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("granted_turns", sa.Numeric(38, 0), nullable=False),
        sa.Column("reserved_turns", sa.Numeric(38, 0), nullable=False),
        sa.Column("consumed_turns", sa.Numeric(38, 0), nullable=False),
        sa.Column("granted_cost_nanos", sa.Numeric(38, 0)),
        sa.Column("reserved_cost_nanos", sa.Numeric(38, 0)),
        sa.Column("consumed_cost_nanos", sa.Numeric(38, 0)),
        sa.Column("turns_watermark", sa.BigInteger(), nullable=False),
        sa.Column("cost_watermark", sa.BigInteger(), nullable=False),
        sa.Column("start_intent_at", sa.DateTime(timezone=True)),
        sa.Column("stop_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("final_usage_at", sa.DateTime(timezone=True)),
        sa.Column("reconcile_worker_id", sa.String(128)),
        sa.Column("reconcile_token_hash", sa.String(64)),
        sa.Column("reconcile_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "execution_key", name="uq_run_budget_execution"),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'START_INTENT', 'SETTLED', 'RELEASED')",
            name="budget_reservation_status",
        ),
        sa.CheckConstraint(
            "granted_turns > 0 AND reserved_turns >= 0 AND consumed_turns >= 0 "
            "AND reserved_turns <= granted_turns",
            name="budget_reservation_turns",
        ),
        sa.CheckConstraint(
            "(granted_cost_nanos IS NULL AND reserved_cost_nanos IS NULL "
            "AND consumed_cost_nanos IS NULL) OR "
            "(granted_cost_nanos IS NOT NULL AND reserved_cost_nanos IS NOT NULL "
            "AND consumed_cost_nanos IS NOT NULL "
            "AND granted_cost_nanos > 0 AND reserved_cost_nanos >= 0 AND consumed_cost_nanos >= 0 "
            "AND reserved_cost_nanos <= granted_cost_nanos)",
            name="budget_reservation_cost",
        ),
        sa.CheckConstraint(
            "(status IN ('RESERVED', 'RELEASED') AND start_intent_at IS NULL) OR "
            "(status IN ('START_INTENT', 'SETTLED') AND start_intent_at IS NOT NULL)",
            name="budget_reservation_start",
        ),
        sa.CheckConstraint(
            "status NOT IN ('SETTLED', 'RELEASED') OR (reserved_turns = 0 AND "
            "(reserved_cost_nanos IS NULL OR reserved_cost_nanos = 0))",
            name="budget_reservation_closed",
        ),
        sa.CheckConstraint(
            "status != 'SETTLED' OR (stop_confirmed_at IS NOT NULL AND final_usage_at IS NOT NULL)",
            name="budget_reservation_settlement",
        ),
        sa.CheckConstraint(
            "status != 'RELEASED' OR (consumed_turns = 0 AND "
            "(consumed_cost_nanos IS NULL OR consumed_cost_nanos = 0))",
            name="budget_reservation_unstarted",
        ),
    )
    op.create_index("ix_run_budget_group", "run_budget_reservations", ["run_id", "group_key"])
    op.create_table(
        "run_budget_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("run_budget_reservations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("receipt_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_checksum", sa.String(64), nullable=False),
        sa.Column("reconcile_worker_id", sa.String(128), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("reservation_id", "receipt_key", name="uq_run_budget_receipt_key"),
        sa.CheckConstraint(
            "kind IN ('USAGE', 'STOP', 'UNSTARTED', 'UNVERIFIABLE')", name="budget_receipt_kind"
        ),
    )


def downgrade() -> None:
    """一件でも勘定/未決/回执があれば回退を止め、旧 Worker の再発行を防ぐ。"""

    op.execute(
        sa.text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM run_budget_accounts)
               OR EXISTS (SELECT 1 FROM run_budget_reservations)
               OR EXISTS (SELECT 1 FROM run_budget_receipts) THEN
                RAISE EXCEPTION 'Run budget ledger must be preserved before downgrade';
            END IF;
        END $$;
    """)
    )
    op.drop_table("run_budget_receipts")
    op.drop_index("ix_run_budget_group", table_name="run_budget_reservations")
    op.drop_table("run_budget_reservations")
    op.drop_table("run_budget_accounts")
