"""会話の credential 版と login 時 role を記録し、切替時の旧会話を失効させる。

Revision ID: 0031_auth_session_credentials
Revises: 0030_run_budget_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_auth_session_credentials"
down_revision: str | None = "0030_run_budget_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """旧 API を停止してから適用し、既存 hash を再解釈せず再ログインを要求する。"""

    op.add_column(
        "auth_sessions",
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("auth_sessions", sa.Column("system_role_at_login", sa.String(16), nullable=True))
    op.create_check_constraint(
        "auth_sessions_credential_version", "auth_sessions", "credential_version IN (1, 2)"
    )
    op.create_check_constraint(
        "auth_sessions_login_role",
        "auth_sessions",
        "system_role_at_login IS NULL OR system_role_at_login IN ('ADMIN', 'USER')",
    )
    op.create_check_constraint(
        "auth_sessions_v2_role_required",
        "auth_sessions",
        "credential_version = 1 OR system_role_at_login IS NOT NULL",
    )
    # 原 token は保存されていない。旧 CSRF を変換せず、失効を DB の事実として残す。
    op.execute(
        "UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE revoked_at IS NULL AND credential_version = 1"
    )


def downgrade() -> None:
    """v2 会話の監査列が不要な空の状態だけで schema を戻す。"""

    # 登出後の行も login 時 role の監査記録であり、revoked を理由に情報を捨てない。
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM auth_sessions WHERE credential_version = 2) "
        "THEN RAISE EXCEPTION 'v2 authentication audit must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_auth_sessions_v2_role_required"), "auth_sessions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_auth_sessions_login_role"), "auth_sessions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_auth_sessions_credential_version"), "auth_sessions", type_="check"
    )
    op.drop_column("auth_sessions", "system_role_at_login")
    op.drop_column("auth_sessions", "credential_version")
