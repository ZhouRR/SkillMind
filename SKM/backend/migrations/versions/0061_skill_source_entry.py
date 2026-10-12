"""共有ディレクトリから選んだ Skill 入口を不変 source に保存する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_skill_source_entry"
down_revision = "0060_document_upload_replace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧 source の入口・hash を推定で補わず、明示選択した新規 source だけ記録する。"""
    op.add_column("skill_sources", sa.Column("entry_path", sa.String(1024), nullable=True))


def downgrade() -> None:
    """選択 source の意味を失う downgrade は拒否し、未使用時だけ列を戻す。"""
    op.execute("LOCK TABLE skill_sources IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM skill_sources WHERE entry_path IS NOT NULL) "
        "THEN RAISE EXCEPTION 'Selected Skill sources prevent downgrade'; END IF; END $$"
    )
    op.drop_column("skill_sources", "entry_path")
