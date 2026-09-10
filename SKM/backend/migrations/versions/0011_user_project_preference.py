"""User が最後に選択した Project preference を追加する。

Revision ID: 0011_user_project_preference
Revises: 0010_identity_and_projects
Create Date: 2026-07-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_user_project_preference"
down_revision: str | None = "0010_identity_and_projects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """User に nullable な Project preference を追加する。"""

    op.add_column("users", sa.Column("preferred_project_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_users_preferred_project_id_projects"),
        "users",
        "projects",
        ["preferred_project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_users_preferred_project_id"),
        "users",
        ["preferred_project_id"],
    )


def downgrade() -> None:
    """Project preference の index、外部 key、column を逆順で削除する。"""

    op.drop_index(op.f("ix_users_preferred_project_id"), table_name="users")
    op.drop_constraint(
        op.f("fk_users_preferred_project_id_projects"),
        "users",
        type_="foreignkey",
    )
    op.drop_column("users", "preferred_project_id")
