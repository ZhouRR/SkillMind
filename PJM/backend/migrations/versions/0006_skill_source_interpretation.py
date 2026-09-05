"""SkillSource と deterministic SkillInterpretation を追加する。

Revision ID: 0006_skill_source_interpretation
Revises: 0005_agent_result_terminal
Create Date: 2026-07-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_skill_source_interpretation"
down_revision: str | None = "0005_agent_result_terminal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """不変 source snapshot と追加式 interpretation preview table を作成する。"""

    op.create_table(
        "skill_sources",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("storage_uri", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("source_version", sa.String(length=128), nullable=True),
        sa.Column("imported_by", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_sources")),
        sa.UniqueConstraint(
            "project_id",
            "content_hash",
            name="uq_skill_sources_project_content_hash",
        ),
    )
    op.create_index(
        op.f("ix_skill_sources_project_id"),
        "skill_sources",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_skill_sources_imported_by"),
        "skill_sources",
        ["imported_by"],
        unique=False,
    )

    op.create_table(
        "skill_interpretations",
        sa.Column("skill_source_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("interpreter_version", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("compatibility_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False),
        sa.Column("questions_json", sa.JSON(), nullable=False),
        sa.Column("diagnostics_json", sa.JSON(), nullable=False),
        sa.Column("normalized_package_json", sa.JSON(), nullable=False),
        sa.Column("manifest_draft_json", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(length=71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["skill_source_id"],
            ["skill_sources.id"],
            name=op.f("fk_skill_interpretations_skill_source_id_skill_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_interpretations")),
        sa.UniqueConstraint(
            "skill_source_id",
            "interpreter_version",
            "checksum",
            name="uq_skill_interpretations_source_version_checksum",
        ),
    )
    op.create_index(
        op.f("ix_skill_interpretations_skill_source_id"),
        "skill_interpretations",
        ["skill_source_id"],
        unique=False,
    )


def downgrade() -> None:
    """Interpretation から source の順に追加 table を削除する。"""

    op.drop_index(
        op.f("ix_skill_interpretations_skill_source_id"),
        table_name="skill_interpretations",
    )
    op.drop_table("skill_interpretations")
    op.drop_index(op.f("ix_skill_sources_imported_by"), table_name="skill_sources")
    op.drop_index(op.f("ix_skill_sources_project_id"), table_name="skill_sources")
    op.drop_table("skill_sources")
