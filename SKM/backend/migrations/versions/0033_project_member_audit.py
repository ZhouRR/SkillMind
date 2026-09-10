"""所属変更の前後と原操作の相関 ID を、既存履歴を補造せず保存する。

Revision ID: 0033_project_member_audit
Revises: 0032_user_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_project_member_audit"
down_revision: str | None = "0032_user_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新規操作だけを監査し、旧 membership から架空の event を生成しない。"""

    op.create_table(
        "project_member_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("previous_status", sa.String(16), nullable=True),
        sa.Column("previous_joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["member_id"], ["project_members.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "(action = 'ADDED' AND status = 'ACTIVE' AND "
            "(previous_status IS NULL OR previous_status = 'REMOVED')) OR "
            "(action = 'REMOVED' AND status = 'REMOVED' AND "
            "previous_status IS NOT NULL AND previous_status = 'ACTIVE')",
            name="project_member_events_transition",
        ),
        sa.CheckConstraint(
            "(previous_status IS NULL) = (previous_joined_at IS NULL)",
            name="project_member_events_previous_state",
        ),
    )
    op.create_index(
        "ix_project_member_events_organization_id", "project_member_events", ["organization_id"]
    )
    op.create_index("ix_project_member_events_project_id", "project_member_events", ["project_id"])


def downgrade() -> None:
    """監査が一件でもあれば table の破棄に進まず、操作証跡の消失を拒否する。"""

    # 存在確認と DROP の間の INSERT も止め、確認後に追加された監査を捨てない。
    op.execute("LOCK TABLE project_member_events IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM project_member_events) "
        "THEN RAISE EXCEPTION 'project member audit must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_index("ix_project_member_events_project_id", table_name="project_member_events")
    op.drop_index("ix_project_member_events_organization_id", table_name="project_member_events")
    op.drop_table("project_member_events")
