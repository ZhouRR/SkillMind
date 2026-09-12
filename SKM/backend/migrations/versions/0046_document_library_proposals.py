"""Project 文書庫の CREATE 提案だけに Integration なしの保存を認める。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_document_library_proposals"
down_revision = "0045_document_effect_uploads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存提案を補填せず、nullable 化と能力/操作の制約を同じ migration で適用する。"""

    op.alter_column("change_proposals", "integration_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        op.f("ck_change_proposals_document_library_integration"),
        "change_proposals",
        "(capability_version = 'document.write/v1' AND operation = 'CREATE' "
        "AND integration_id IS NULL) OR "
        "(capability_version <> 'document.write/v1' AND integration_id IS NOT NULL)",
    )


def downgrade() -> None:
    """旧版で読めない提案が一つでも残れば排他 lock の下で降級を拒否する。"""

    op.execute("LOCK TABLE change_proposals IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM change_proposals WHERE integration_id IS NULL) THEN
            RAISE EXCEPTION 'Document library proposals prevent downgrade';
          END IF;
        END $$;
    """)
    op.drop_constraint(
        op.f("ck_change_proposals_document_library_integration"), "change_proposals", type_="check"
    )
    op.alter_column("change_proposals", "integration_id", existing_type=sa.Uuid(), nullable=False)
