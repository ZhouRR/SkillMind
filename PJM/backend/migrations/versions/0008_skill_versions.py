"""Skill、SkillVersion、RuntimeManifest の不変 publish spine を追加する。

Revision ID: 0008_skill_versions
Revises: 0007_evaluations
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_skill_versions"
down_revision: str | None = "0007_evaluations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Version identity、source binding、frozen Manifest table を依存順に作成する。"""

    op.create_table(
        "skills",
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skills")),
    )
    op.create_index(op.f("ix_skills_key"), "skills", ["key"], unique=True)
    op.create_table(
        "skill_versions",
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("skill_source_id", sa.Uuid(), nullable=False),
        sa.Column("interpretation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("gate_report_json", sa.JSON(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skill_source_id"], ["skill_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["interpretation_id"], ["skill_interpretations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_skill_versions")),
        sa.UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
        sa.UniqueConstraint("interpretation_id", name="uq_skill_versions_interpretation"),
    )
    for column in ("skill_id", "skill_source_id", "interpretation_id", "published_by"):
        op.create_index(op.f(f"ix_skill_versions_{column}"), "skill_versions", [column])
    op.create_table(
        "runtime_manifests",
        sa.Column("interpretation_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_version", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["interpretation_id"], ["skill_interpretations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_manifests")),
    )
    op.create_index(
        op.f("ix_runtime_manifests_interpretation_id"), "runtime_manifests", ["interpretation_id"]
    )
    op.create_index(
        op.f("ix_runtime_manifests_skill_version_id"),
        "runtime_manifests",
        ["skill_version_id"],
        unique=True,
    )


def downgrade() -> None:
    """Manifest、Version、Skill の順に publish spine を削除する。"""

    op.drop_table("runtime_manifests")
    op.drop_table("skill_versions")
    op.drop_table("skills")
