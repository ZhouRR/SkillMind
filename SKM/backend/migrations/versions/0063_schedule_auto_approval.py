"""定期実行で明示された自動承認を保存し、原 occurrence の要求へ固定する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0063_schedule_auto_approval'
down_revision = '0062_http_resource_roots'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """既存設定は同意なしに変更せず、新規保存から自動承認の有無を保持する。"""
    op.add_column('task_schedules', sa.Column('auto_approve', sa.Boolean(), nullable=False,
                                             server_default=sa.false()))


def downgrade() -> None:
    """自動承認の保存設定がある間は、発火の意味を失う列削除を拒否する。"""
    op.execute('LOCK TABLE task_schedules IN ACCESS EXCLUSIVE MODE')
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM task_schedules WHERE auto_approve) "
               "THEN RAISE EXCEPTION 'Automatic approval schedules prevent downgrade'; "
               "END IF; END $$")
    op.drop_column('task_schedules', 'auto_approve')
