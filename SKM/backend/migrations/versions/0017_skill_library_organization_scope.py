"""Skill 資産の Organization 作用域と Project 有効化関係を追加する (expand)。

Revision ID: 0017_skill_library_org_scope
Revises: 0015_skill_compositions
Create Date: 2026-07-17

docs/01 §16 T2 の純増量段階。既存の `skill_sources.project_id` と唯一制約はそのまま残し、
作用域の強制点も変更しない。したがって本 migration 適用後も各 Project の TaskCatalog 内容と
既存 Run/Result の可読性は一切変わらない。旧列の削除と唯一制約の organization 単位への移動は、
強制点を有効化関係へ切り替える T3 で行う。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_skill_library_org_scope"
down_revision: str | None = "0015_skill_compositions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """organization_id の追加・回填と、有効化関係 table の作成・播種を行う。"""

    _add_skill_source_organization()
    _create_project_skill_versions()
    _seed_existing_enablements()


def _add_skill_source_organization() -> None:
    """SkillSource へ organization_id を足し、所属 Project 経由で回填する。

    回填してから NOT NULL へ締めるのは、既存行がある環境で NOT NULL 列を直接追加できないため。
    """

    op.add_column("skill_sources", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE skill_sources
            SET organization_id = projects.organization_id
            FROM projects
            WHERE projects.id = skill_sources.project_id
            """
        )
    )
    # 回填できない行が残ったまま NOT NULL にすると migration が不明瞭に失敗する。所属 Project の
    # 無い SkillSource は作用域を決められないため、ここで明示的に止める。
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE orphaned bigint;
            BEGIN
                SELECT count(*) INTO orphaned FROM skill_sources WHERE organization_id IS NULL;
                IF orphaned > 0 THEN
                    RAISE EXCEPTION
                        'skill_sources has % row(s) whose project is missing; '
                        'resolve them before upgrading', orphaned;
                END IF;
            END $$
            """
        )
    )
    op.alter_column("skill_sources", "organization_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_skill_sources_organization_id_organizations"),
        "skill_sources",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_skill_sources_organization_id"), "skill_sources", ["organization_id"]
    )


def _create_project_skill_versions() -> None:
    """Project が精確 PUBLISHED 版を有効化した関係 table を作成する。"""

    op.create_table(
        "project_skill_versions",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("enabled_by", sa.Uuid(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        # 停用は行を消さず disabled_at を立てる。誰がいつ実行面を広げ、いつ畳んだかは監査事実。
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_project_skill_versions_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name=op.f("fk_project_skill_versions_skill_version_id_skill_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_skill_versions")),
        sa.UniqueConstraint(
            "project_id",
            "skill_version_id",
            name=op.f("uq_project_skill_versions_project_version"),
        ),
    )
    op.create_index(
        op.f("ix_project_skill_versions_project_id"), "project_skill_versions", ["project_id"]
    )
    op.create_index(
        op.f("ix_project_skill_versions_skill_version_id"),
        "project_skill_versions",
        ["skill_version_id"],
    )


def _seed_existing_enablements() -> None:
    """既存 PUBLISHED 版を、その source が属していた Project へ有効化として播種する。

    T3 が作用域強制を有効化関係へ切り替えた瞬間に、播種が無いと全 Project の TaskCatalog が空に
    なる。したがってこの播種が「移行前後で実行可能 task 集合が変わらない」門禁の実体である。

    `enabled_by` は source の `imported_by` を使う。既定 ADMIN を捏造すると、実際には行われて
    いない承認行為を監査事実として残すことになるため、実在する既知の actor だけを使う。
    """

    op.execute(
        sa.text(
            """
            INSERT INTO project_skill_versions
                (id, project_id, skill_version_id, enabled_by, enabled_at, disabled_at)
            SELECT
                gen_random_uuid(),
                skill_sources.project_id,
                skill_versions.id,
                skill_sources.imported_by,
                COALESCE(skill_versions.published_at, skill_versions.created_at),
                NULL
            FROM skill_versions
            JOIN skill_sources ON skill_sources.id = skill_versions.skill_source_id
            WHERE skill_versions.status = 'PUBLISHED'
            ON CONFLICT ON CONSTRAINT uq_project_skill_versions_project_version DO NOTHING
            """
        )
    )


def downgrade() -> None:
    """有効化関係 table と organization_id を取り除く。"""

    op.drop_index(
        op.f("ix_project_skill_versions_skill_version_id"), table_name="project_skill_versions"
    )
    op.drop_index(
        op.f("ix_project_skill_versions_project_id"), table_name="project_skill_versions"
    )
    op.drop_table("project_skill_versions")
    op.drop_index(op.f("ix_skill_sources_organization_id"), table_name="skill_sources")
    op.drop_constraint(
        op.f("fk_skill_sources_organization_id_organizations"),
        "skill_sources",
        type_="foreignkey",
    )
    op.drop_column("skill_sources", "organization_id")
