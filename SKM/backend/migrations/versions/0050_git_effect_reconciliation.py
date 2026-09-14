"""Git 原 commit の只読照会を既存要求台帳へ追加する。"""

from __future__ import annotations

from alembic import op

revision = "0050_git_effect_reconciliation"
down_revision = "0049_run_start_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存 DB/文書の観測を保ち、Git commit 種別を受け入れる。"""
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


def downgrade() -> None:
    """Git 観測が残る場合は削除せず、旧制約への復帰を拒否する。"""
    op.drop_constraint(
        op.f("ck_effect_reconciliation_requests_effect_reconciliation_kind"),
        "effect_reconciliation_requests",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_effect_reconciliation_requests_effect_reconciliation_kind"),
        "effect_reconciliation_requests",
        "kind IN ('DATABASE_TRANSACTION', 'DOCUMENT_OBJECT')",
    )
