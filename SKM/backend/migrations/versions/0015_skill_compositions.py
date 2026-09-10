"""SkillComposition(module 等)と Project 有効化の table を追加する。

Revision ID: 0015_skill_compositions
Revises: 0014_project_documents
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_skill_compositions"
down_revision: str | None = "0014_project_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """組合本体、SkillVersion 束縛、Project 有効化の三 table を作成する。"""

    op.create_table(
        "skill_compositions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("presentation", sa.String(16), nullable=False),
        sa.Column("behavior_prompt", sa.Text(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "presentation IN ('role', 'module', 'task_group')",
            name=op.f("skill_compositions_presentation"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')", name=op.f("skill_compositions_status")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_skill_compositions_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_compositions")),
    )
    op.create_index(
        op.f("ix_skill_compositions_organization_id"),
        "skill_compositions",
        ["organization_id"],
    )

    op.create_table(
        "skill_composition_items",
        sa.Column("composition_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_override_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["composition_id"],
            ["skill_compositions.id"],
            name=op.f("fk_skill_composition_items_composition_id_skill_compositions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name=op.f("fk_skill_composition_items_skill_version_id_skill_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_composition_items")),
        # 同一組合内で同じ SkillVersion を二重に束縛しない。
        sa.UniqueConstraint(
            "composition_id",
            "skill_version_id",
            name="uq_skill_composition_items_composition_version",
        ),
    )
    op.create_index(
        op.f("ix_skill_composition_items_composition_id"),
        "skill_composition_items",
        ["composition_id"],
    )
    op.create_index(
        op.f("ix_skill_composition_items_skill_version_id"),
        "skill_composition_items",
        ["skill_version_id"],
    )

    op.create_table(
        "project_compositions",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("composition_id", sa.Uuid(), nullable=False),
        sa.Column("enabled_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_compositions_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["composition_id"],
            ["skill_compositions.id"],
            name=op.f("fk_project_compositions_composition_id_skill_compositions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_compositions")),
        sa.UniqueConstraint(
            "project_id",
            "composition_id",
            name="uq_project_compositions_project_composition",
        ),
    )
    op.create_index(
        op.f("ix_project_compositions_project_id"),
        "project_compositions",
        ["project_id"],
    )
    op.create_index(
        op.f("ix_project_compositions_composition_id"),
        "project_compositions",
        ["composition_id"],
    )


def downgrade() -> None:
    """組合関連の三 table を依存順に削除する。"""

    op.drop_index(
        op.f("ix_project_compositions_composition_id"), table_name="project_compositions"
    )
    op.drop_index(op.f("ix_project_compositions_project_id"), table_name="project_compositions")
    op.drop_table("project_compositions")
    op.drop_index(
        op.f("ix_skill_composition_items_skill_version_id"),
        table_name="skill_composition_items",
    )
    op.drop_index(
        op.f("ix_skill_composition_items_composition_id"),
        table_name="skill_composition_items",
    )
    op.drop_table("skill_composition_items")
    op.drop_index(
        op.f("ix_skill_compositions_organization_id"), table_name="skill_compositions"
    )
    op.drop_table("skill_compositions")
