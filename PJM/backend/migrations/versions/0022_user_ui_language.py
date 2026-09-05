"""User の UI 言語 preference を追加する。

Revision ID: 0022_user_ui_language
Revises: 0021_controlled_effects
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_user_ui_language"
down_revision: str | None = "0021_controlled_effects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """User に nullable な UI 言語 preference を追加する。

    NULL は「未設定=browser 言語へ追従」を表す。値は docs/01 §17 の言語集合に固定する。
    """

    op.add_column("users", sa.Column("ui_language", sa.String(length=8), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_users_ui_language"),
        "users",
        "ui_language IN ('zh', 'ja', 'en')",
    )


def downgrade() -> None:
    """UI 言語 preference の制約と column を逆順で削除する。"""

    op.drop_constraint(op.f("ck_users_users_ui_language"), "users", type_="check")
    op.drop_column("users", "ui_language")
