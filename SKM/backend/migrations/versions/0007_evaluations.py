"""不変 Result に対する追加式 Evaluation table を追加する。

Revision ID: 0007_evaluations
Revises: 0006_skill_source_interpretation
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_evaluations"
down_revision: str | None = "0006_skill_source_interpretation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Result を更新せず人工評価を複数追加できる table を作成する。"""

    op.create_table(
        "evaluations",
        sa.Column("result_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("revision_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "rating >= 1 AND rating <= 5", name=op.f("ck_evaluations_rating_range")
        ),
        sa.CheckConstraint(
            "verdict IN ('accurate', 'partially_accurate', 'inaccurate', 'uncertain')",
            name=op.f("ck_evaluations_verdict_value"),
        ),
        sa.ForeignKeyConstraint(
            ["result_id"],
            ["run_results.id"],
            name=op.f("fk_evaluations_result_id_run_results"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluations")),
    )
    op.create_index(op.f("ix_evaluations_result_id"), "evaluations", ["result_id"])
    op.create_index(op.f("ix_evaluations_user_id"), "evaluations", ["user_id"])


def downgrade() -> None:
    """Evaluation index と table を依存関係の逆順で削除する。"""

    op.drop_index(op.f("ix_evaluations_user_id"), table_name="evaluations")
    op.drop_index(op.f("ix_evaluations_result_id"), table_name="evaluations")
    op.drop_table("evaluations")
