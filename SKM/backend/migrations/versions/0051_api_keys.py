"""API key の独立した資格版と管理 metadata を追加する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_api_keys"
down_revision = "0050_git_effect_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存 browser session を変換・失効させず、key のみを新しい版にする。"""
    op.drop_constraint(
        op.f("ck_auth_sessions_auth_sessions_credential_version"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        "auth_sessions_credential_version", "auth_sessions", "credential_version IN (1, 2, 3)"
    )
    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            sa.Uuid(),
            sa.ForeignKey("auth_sessions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
    )
    op.create_index("ix_api_keys_organization_id", "api_keys", ["organization_id"])


def downgrade() -> None:
    """発行済み key と原要求参照がある場合は資格の意味を捨てない。"""
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM auth_sessions WHERE credential_version = 3) "
        "THEN RAISE EXCEPTION 'API key audit must be preserved before downgrade'; END IF; END $$;"
    )
    op.drop_table("api_keys")
    op.drop_constraint(
        op.f("ck_auth_sessions_auth_sessions_credential_version"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        "auth_sessions_credential_version", "auth_sessions", "credential_version IN (1, 2)"
    )
