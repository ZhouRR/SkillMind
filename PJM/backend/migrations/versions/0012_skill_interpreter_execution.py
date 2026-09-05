"""Model interpretation 実行の report と audit envelope 列を追加する。

Revision ID: 0012_skill_interpreter_execution
Revises: 0011_user_project_preference
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_skill_interpreter_execution"
down_revision: str | None = "0011_user_project_preference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """SkillInterpretation に nullable な report と execution envelope 列を追加する。"""

    # Deterministic parser preview は両列を NULL に保つため、既存行の backfill は不要。
    op.add_column(
        "skill_interpretations",
        sa.Column("report_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "skill_interpretations",
        sa.Column("execution_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """追加した execution envelope と report 列を逆順で削除する。"""

    op.drop_column("skill_interpretations", "execution_json")
    op.drop_column("skill_interpretations", "report_json")
