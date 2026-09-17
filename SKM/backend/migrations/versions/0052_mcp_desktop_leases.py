"""MCP の Run 専有と不明操作を永続化する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_mcp_desktop_leases"
down_revision = "0051_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存 Run を変更せず、初回 MCP 使用時だけ専有行を作る。"""
    op.drop_constraint(
        op.f("ck_effect_reconciliation_requests_effect_reconciliation_kind"),
        "effect_reconciliation_requests",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_effect_reconciliation_requests_effect_reconciliation_kind"),
        "effect_reconciliation_requests",
        "kind IN ('DATABASE_TRANSACTION', 'DOCUMENT_OBJECT', 'GIT_COMMIT', 'MCP_OPERATION')",
    )
    op.create_table(
        "mcp_desktop_leases",
        sa.Column("endpoint_hash", sa.String(64), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "pending_effect_id",
            sa.Uuid(),
            sa.ForeignKey("effect_executions.id", ondelete="RESTRICT"),
        ),
    )


def downgrade() -> None:
    """終了未確認の記録を rollback で消さない。"""
    op.execute(
        "LOCK TABLE mcp_desktop_leases, effect_reconciliation_requests IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM mcp_desktop_leases)
          OR EXISTS (SELECT 1 FROM effect_reconciliation_requests WHERE kind = 'MCP_OPERATION')
        THEN RAISE EXCEPTION 'MCP ownership or observations prevent downgrade'; END IF;
        END $$""")
    op.drop_constraint(
        op.f("ck_effect_reconciliation_requests_effect_reconciliation_kind"),
        "effect_reconciliation_requests",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_effect_reconciliation_requests_effect_reconciliation_kind"),
        "effect_reconciliation_requests",
        "kind IN ('DATABASE_TRANSACTION', 'DOCUMENT_OBJECT', 'GIT_COMMIT')",
    )
    op.drop_table("mcp_desktop_leases")
