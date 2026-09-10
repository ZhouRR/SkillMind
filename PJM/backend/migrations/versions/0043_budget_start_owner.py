"""原調整者の起動所有権だけを保存し、旧実行や未起動証明を補造しない。

Revision ID: 0043_budget_start_owner
Revises: 0042_document_upload_closures
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_budget_start_owner"
down_revision: str | None = "0042_document_upload_closures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """旧束縛は所有者なしのまま保持し、新しい所有権は完全な原束縛を必須とする。"""

    op.add_column(
        "run_budget_reservations",
        sa.Column("invocation_start_owner_hash", sa.String(71), nullable=True),
    )
    op.create_check_constraint(
        "budget_invocation_start_owner",
        "run_budget_reservations",
        "invocation_start_owner_hash IS NULL OR "
        "(invocation_id IS NOT NULL AND invocation_json IS NOT NULL "
        "AND invocation_checksum IS NOT NULL "
        "AND invocation_start_owner_hash ~ '^sha256:[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    """所有権の痕跡を消す回退は、終態や offline SQL を含めて拒否する。"""

    op.execute("LOCK TABLE run_budget_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM run_budget_reservations "
        "WHERE invocation_start_owner_hash IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Budget start ownership must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint(
        op.f("ck_run_budget_reservations_budget_invocation_start_owner"),
        "run_budget_reservations",
        type_="check",
    )
    op.drop_column("run_budget_reservations", "invocation_start_owner_hash")
