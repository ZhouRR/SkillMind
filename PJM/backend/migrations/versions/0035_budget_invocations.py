"""原予約への invocation 束縛と原始観察を保存し、過去の実行や消費を補造しない。

Revision ID: 0035_budget_invocations
Revises: 0034_project_row_version
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_budget_invocations"
down_revision: str | None = "0034_project_row_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """未束縛の旧予約は全 SQL NULL のまま保持し、新しい観察にも元予約を必須とする。"""

    op.add_column("run_budget_reservations", sa.Column("invocation_id", sa.Uuid(), nullable=True))
    op.add_column(
        "run_budget_reservations",
        sa.Column("invocation_json", sa.JSON(none_as_null=True), nullable=True),
    )
    op.add_column(
        "run_budget_reservations", sa.Column("invocation_checksum", sa.String(64), nullable=True)
    )
    op.create_unique_constraint(
        "uq_run_budget_invocation_id", "run_budget_reservations", ["invocation_id"]
    )
    op.create_unique_constraint(
        "uq_run_budget_reservation_invocation", "run_budget_reservations", ["id", "invocation_id"]
    )
    op.create_check_constraint(
        "budget_invocation_binding",
        "run_budget_reservations",
        "(invocation_id IS NULL AND invocation_json IS NULL AND invocation_checksum IS NULL) OR "
        "(invocation_id IS NOT NULL AND invocation_json IS NOT NULL "
        "AND invocation_checksum IS NOT NULL)",
    )
    op.create_table(
        "run_budget_observations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column("observation_key", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_checksum", sa.String(64), nullable=False),
        sa.Column("reconcile_worker_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reservation_id", "invocation_id"],
            ["run_budget_reservations.id", "run_budget_reservations.invocation_id"],
            name="fk_run_budget_observation_invocation",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "reservation_id", "observation_key", name="uq_run_budget_observation_key"
        ),
    )


def downgrade() -> None:
    """監査の存在検査と DDL の隙間を閉じ、束縛だけが残った場合も回退を拒否する。"""

    op.execute(
        "LOCK TABLE run_budget_reservations, run_budget_observations IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM run_budget_reservations WHERE invocation_id IS NOT NULL "
        "OR invocation_json IS NOT NULL OR invocation_checksum IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM run_budget_observations) THEN "
        "RAISE EXCEPTION 'Budget invocation bindings and raw observations must be preserved "
        "before downgrade'; END IF; END $$;"
    )
    op.drop_table("run_budget_observations")
    op.drop_constraint(
        op.f("ck_run_budget_reservations_budget_invocation_binding"),
        "run_budget_reservations",
        type_="check",
    )
    op.drop_constraint(
        "uq_run_budget_reservation_invocation", "run_budget_reservations", type_="unique"
    )
    op.drop_constraint("uq_run_budget_invocation_id", "run_budget_reservations", type_="unique")
    op.drop_column("run_budget_reservations", "invocation_checksum")
    op.drop_column("run_budget_reservations", "invocation_json")
    op.drop_column("run_budget_reservations", "invocation_id")
