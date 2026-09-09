"""User の楽観版と、credential を含めない追加式管理監査を保存する。

Revision ID: 0032_user_lifecycle
Revises: 0031_auth_session_credentials
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_user_lifecycle"
down_revision: str | None = "0031_auth_session_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """既存 user の版を一に揃え、存在しなかった操作履歴は補造しない。"""

    op.add_column(
        "users", sa.Column("row_version", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_check_constraint("users_row_version_positive", "users", "row_version >= 1")
    op.create_table(
        "user_security_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("previous_role", sa.String(16), nullable=True),
        sa.Column("previous_status", sa.String(16), nullable=True),
        sa.Column("system_role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revoked_sessions", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_id", "row_version", name="uq_user_security_events_version"),
        sa.CheckConstraint(
            "action IN ('CREATED', 'UPDATED', 'PASSWORD_CHANGED', 'SESSIONS_REVOKED')",
            name="user_security_events_action",
        ),
        sa.CheckConstraint("row_version >= 1", name="user_security_events_version_positive"),
        sa.CheckConstraint(
            "revoked_sessions >= 0", name="user_security_events_revoked_nonnegative"
        ),
        sa.CheckConstraint(
            "previous_role IS NULL OR previous_role IN ('ADMIN', 'USER')",
            name="user_security_events_previous_role",
        ),
        sa.CheckConstraint(
            "previous_status IS NULL OR previous_status IN ('ACTIVE', 'DISABLED')",
            name="user_security_events_previous_status",
        ),
        sa.CheckConstraint("system_role IN ('ADMIN', 'USER')", name="user_security_events_role"),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="user_security_events_status"),
    )
    op.create_index(
        "ix_user_security_events_organization_id", "user_security_events", ["organization_id"]
    )
    op.create_index("ix_user_security_events_user_id", "user_security_events", ["user_id"])


def downgrade() -> None:
    """失効済み会話に対応する操作を含め、監査行があれば復元を拒否する。"""

    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM user_security_events) "
        "THEN RAISE EXCEPTION 'user security audit must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_index("ix_user_security_events_user_id", table_name="user_security_events")
    op.drop_index("ix_user_security_events_organization_id", table_name="user_security_events")
    op.drop_table("user_security_events")
    op.drop_constraint(op.f("ck_users_users_row_version_positive"), "users", type_="check")
    op.drop_column("users", "row_version")
