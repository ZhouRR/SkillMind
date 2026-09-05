"""SkillSource の完全な file index を保存し、object storage の再構築を可能にする。

Revision ID: 0028_skill_source_file_index
Revises: 0027_subagent_sessions
Create Date: 2026-07-28

Upload source は text snapshot と binary bundle を分離して保持する。hash は全 file の index
から作られるため、解釈時に同じ bundle を再構築できなければ source の不変性を検証できない。
既存行は deterministic preview に保存された normalized package の file index を移し、preview
が無い歴史行だけは空 index として fail closed できるようにする。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_skill_source_file_index"
down_revision: str | None = "0027_subagent_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """全 SkillSource へ file index を追加し、既存 deterministic preview から backfill する。"""

    op.add_column(
        "skill_sources",
        sa.Column("source_file_index_json", sa.JSON(), nullable=True),
    )
    # source_snapshot_json は text だけを持つため、binary を含む元の index は同一 transaction で
    # 保存された deterministic interpretation から復元する。見つからない歴史行は空のままにし、
    # 実行時に黙って text-only と解釈せず fail closed させる。
    op.execute(
        sa.text(
            """
            UPDATE skill_sources AS sources
            SET source_file_index_json = COALESCE(
                (
                    SELECT interpretations.normalized_package_json -> 'source' -> 'files'
                    FROM skill_interpretations AS interpretations
                    WHERE interpretations.skill_source_id = sources.id
                      AND interpretations.origin = 'deterministic_parser'
                    ORDER BY interpretations.created_at ASC
                    LIMIT 1
                ),
                CAST('[]' AS json)
            )
            """
        )
    )
    op.alter_column(
        "skill_sources",
        "source_file_index_json",
        nullable=False,
        server_default=sa.text("CAST('[]' AS json)"),
    )


def downgrade() -> None:
    """追加した immutable file index を削除する。"""

    op.drop_column("skill_sources", "source_file_index_json")
