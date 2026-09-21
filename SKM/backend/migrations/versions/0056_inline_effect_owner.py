"""短い自動承認操作の原 SDK 呼出しと所有 Attempt を保存する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_inline_effect_owner"
down_revision = "0055_run_deletion_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存提案は deferred のまま、直接交付の内部記録だけを追加する。"""
    op.add_column("run_attempts", sa.Column("inline_proposal_id", sa.Uuid(), nullable=True))
    op.add_column("change_proposals", sa.Column("inline_owner_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """原操作の復旧 identity が存在する間は削除しない。"""
    op.execute("LOCK TABLE change_proposals IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM change_proposals WHERE inline_owner_json IS NOT NULL) THEN RAISE EXCEPTION 'Inline operation history prevents downgrade'; END IF; END $$"
    )
    op.drop_column("change_proposals", "inline_owner_json")
    op.drop_column("run_attempts", "inline_proposal_id")
