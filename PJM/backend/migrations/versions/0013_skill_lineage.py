"""Model interpretation の append-only revision lineage 列を追加する。

Revision ID: 0013_skill_lineage
Revises: 0012_skill_interpreter_execution
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_skill_lineage"
down_revision: str | None = "0012_skill_interpreter_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """SkillInterpretation に nullable な parent lineage と adjustment 列を追加する。"""

    # reinterpretation は親を書き換えず新しい行を追加するため、既存行は NULL のままにする。
    op.add_column(
        "skill_interpretations",
        sa.Column("parent_interpretation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "skill_interpretations",
        sa.Column("adjustment_json", sa.JSON(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_skill_interpretations_parent_interpretation_id_skill_interpretations"),
        "skill_interpretations",
        "skill_interpretations",
        ["parent_interpretation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_skill_interpretations_parent_interpretation_id"),
        "skill_interpretations",
        ["parent_interpretation_id"],
    )


def downgrade() -> None:
    """追加した lineage index、外部 key、adjustment/parent 列を逆順で削除する。"""

    op.drop_index(
        op.f("ix_skill_interpretations_parent_interpretation_id"),
        table_name="skill_interpretations",
    )
    op.drop_constraint(
        op.f("fk_skill_interpretations_parent_interpretation_id_skill_interpretations"),
        "skill_interpretations",
        type_="foreignkey",
    )
    op.drop_column("skill_interpretations", "adjustment_json")
    op.drop_column("skill_interpretations", "parent_interpretation_id")
