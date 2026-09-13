"""Run 開始時の同意を手動承認と区別して監査記録に保存する。"""

from __future__ import annotations

from alembic import op

revision = "0049_run_start_approval"
down_revision = "0048_document_effect_protocol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存の承認は変更せず、新しい判断源だけを許容する。"""

    op.drop_constraint(
        op.f("ck_change_approvals_change_approvals_source"), "change_approvals", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_change_approvals_change_approvals_source"),
        "change_approvals",
        "source IN ('USER', 'PREAUTHORIZATION', 'RUN_START')",
    )


def downgrade() -> None:
    """新判断源が残る場合は CHECK で拒否し、履歴を消去・偽装しない。"""

    op.execute("""
        DO $$ BEGIN
            LOCK TABLE runs, change_approvals IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (
                SELECT 1 FROM runs
                WHERE task_snapshot_json->'creation_request'->>'auto_approve' = 'true'
            ) OR EXISTS (SELECT 1 FROM change_approvals WHERE source = 'RUN_START') THEN
                RAISE EXCEPTION 'Run start consent cannot be represented by the previous version';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        op.f("ck_change_approvals_change_approvals_source"), "change_approvals", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_change_approvals_change_approvals_source"),
        "change_approvals",
        "source IN ('USER', 'PREAUTHORIZATION')",
    )
