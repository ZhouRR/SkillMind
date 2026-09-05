"""Legacy JAF seed を task discovery から退役する。

Revision ID: 0016_retire_legacy_jaf_seed
Revises: 0015_skill_compositions
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_retire_legacy_jaf_seed"
down_revision: str | None = "0015_skill_compositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_JAF_SKILL_VERSION_ID = "00000000-0000-4000-8000-000000000203"


def upgrade() -> None:
    """Historical row を削除せず DEPRECATED にし、新規 Run の task catalog から除外する。"""

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE skill_versions
            SET status = 'DEPRECATED'
            WHERE id = CAST(:version_id AS uuid)
              AND status = 'PUBLISHED'
            """
        ),
        {"version_id": _LEGACY_JAF_SKILL_VERSION_ID},
    )
    # 既存組合が seed を参照していても、退役後の新規 Project binding へ露出させない。
    connection.execute(
        sa.text(
            """
            UPDATE skill_composition_items
            SET enabled = FALSE
            WHERE skill_version_id = CAST(:version_id AS uuid)
            """
        ),
        {"version_id": _LEGACY_JAF_SKILL_VERSION_ID},
    )


def downgrade() -> None:
    """Rollback 時だけ legacy seed の公開状態を復元する。組合の選択状態は復元しない。"""

    op.get_bind().execute(
        sa.text(
            """
            UPDATE skill_versions
            SET status = 'PUBLISHED'
            WHERE id = CAST(:version_id AS uuid)
              AND status = 'DEPRECATED'
            """
        ),
        {"version_id": _LEGACY_JAF_SKILL_VERSION_ID},
    )
