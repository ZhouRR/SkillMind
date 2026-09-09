"""Project の metadata/status に原版比較を導入し、過去の編集回数を補造しない。

Revision ID: 0034_project_row_version
Revises: 0033_project_member_audit
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_project_row_version"
down_revision: str | None = "0033_project_member_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """既存 Project を版一として引き継ぎ、新旧 writer の混在を許可しない。"""

    op.add_column(
        "projects", sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "projects_row_version_range", "projects", "row_version >= 1 AND row_version <= 2147483647",
    )


def downgrade() -> None:
    """利用済みの版を消して一へ戻さない。全 writer の停止は lock と別途必要。"""

    op.execute("LOCK TABLE projects IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM projects WHERE row_version > 1) "
        "THEN RAISE EXCEPTION 'used project versions must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint(op.f("ck_projects_projects_row_version_range"), "projects", type_="check")
    op.drop_column("projects", "row_version")
