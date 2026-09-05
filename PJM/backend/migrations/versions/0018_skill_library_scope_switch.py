"""Skill library の作用域を Organization と Project 有効化関係へ切り替える。

Revision ID: 0018_skill_library_scope
Revises: 0017_skill_library_org_scope
Create Date: 2026-07-18

docs/01 §16 T3。SkillSource の旧 project anchor を撤去し、Organization 内の資産 identity と
ProjectSkillVersion の明示的な可視性を正本にする。既存 source の重複や組織を跨ぐ有効化を
黙って統合すると別資産・別権限を同一視するため、収縮前に検出して fail closed とする。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_skill_library_scope"
down_revision: str | None = "0017_skill_library_org_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """組織資産 identity を補完してから旧 Project anchor を収縮する。"""

    _guard_source_convergence()
    _guard_enablement_scope()
    _scope_skills_to_organization()
    _contract_skill_sources()


def _guard_source_convergence() -> None:
    """同一 Organization 内の同一 content を自動統合せず migration を止める。"""

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE conflicts bigint;
            BEGIN
                SELECT count(*) INTO conflicts
                FROM (
                    SELECT organization_id, content_hash
                    FROM skill_sources
                    GROUP BY organization_id, content_hash
                    HAVING count(*) > 1
                ) duplicated;
                IF conflicts > 0 THEN
                    RAISE EXCEPTION
                        'skill_sources has % duplicate organization/content scope(s); '
                        'resolve source identities before upgrading', conflicts;
                END IF;
            END $$
            """
        )
    )


def _guard_enablement_scope() -> None:
    """既存有効化が PUBLISHED かつ同一 Organization 内であることを確認する。"""

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE invalid bigint;
            BEGIN
                SELECT count(*) INTO invalid
                FROM project_skill_versions bindings
                JOIN projects ON projects.id = bindings.project_id
                JOIN skill_versions versions ON versions.id = bindings.skill_version_id
                JOIN skill_sources sources ON sources.id = versions.skill_source_id
                WHERE versions.status <> 'PUBLISHED'
                   OR projects.organization_id <> sources.organization_id;
                IF invalid > 0 THEN
                    RAISE EXCEPTION
                        'project_skill_versions has % invalid cross-scope or unpublished row(s)',
                        invalid;
                END IF;
            END $$
            """
        )
    )


def _scope_skills_to_organization() -> None:
    """Skill identity を version source と同じ Organization に固定する。"""

    op.add_column("skills", sa.Column("organization_id", sa.Uuid(), nullable=True))
    # 一つの Skill identity が複数 Organization の version を束ねていた場合、どちらへ帰属させるかを
    # 推測できない。分割には新 identity と参照更新が必要なので収縮 migration では行わない。
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE conflicts bigint;
            BEGIN
                SELECT count(*) INTO conflicts
                FROM (
                    SELECT versions.skill_id
                    FROM skill_versions versions
                    JOIN skill_sources sources ON sources.id = versions.skill_source_id
                    GROUP BY versions.skill_id
                    HAVING count(DISTINCT sources.organization_id) > 1
                ) mixed;
                IF conflicts > 0 THEN
                    RAISE EXCEPTION
                        'skills has % identity row(s) spanning multiple organizations; '
                        'split them before upgrading', conflicts;
                END IF;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE skills
            SET organization_id = scoped.organization_id
            FROM (
                SELECT versions.skill_id, min(sources.organization_id::text)::uuid organization_id
                FROM skill_versions versions
                JOIN skill_sources sources ON sources.id = versions.skill_source_id
                GROUP BY versions.skill_id
            ) scoped
            WHERE scoped.skill_id = skills.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE orphaned bigint;
            BEGIN
                SELECT count(*) INTO orphaned FROM skills WHERE organization_id IS NULL;
                IF orphaned > 0 THEN
                    RAISE EXCEPTION
                        'skills has % row(s) without a version/source organization; '
                        'resolve them before upgrading', orphaned;
                END IF;
            END $$
            """
        )
    )
    op.alter_column("skills", "organization_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_skills_organization_id_organizations"),
        "skills",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(op.f("ix_skills_organization_id"), "skills", ["organization_id"])
    op.drop_index(op.f("ix_skills_key"), table_name="skills")
    op.create_index(op.f("ix_skills_key"), "skills", ["key"], unique=False)
    op.create_unique_constraint(
        op.f("uq_skills_organization_key"), "skills", ["organization_id", "key"]
    )


def _contract_skill_sources() -> None:
    """Source の旧 project_id を削除し organization/content identity へ収縮する。"""

    op.drop_constraint(
        "uq_skill_sources_project_content_hash", "skill_sources", type_="unique"
    )
    op.drop_index(op.f("ix_skill_sources_project_id"), table_name="skill_sources")
    op.drop_column("skill_sources", "project_id")
    op.create_unique_constraint(
        op.f("uq_skill_sources_organization_content_hash"),
        "skill_sources",
        ["organization_id", "content_hash"],
    )


def downgrade() -> None:
    """旧 Project anchor を復元できる場合だけ 0017 の形へ戻す。

    T3 後は一つの source/version を複数 Project が共有できるため、一般には単一
    project_id へ戻せない。曖昧な状態を任意の Project へ割り当てると越権になるので、
    backup 復元が必要なケースは明示的に止める。
    """

    _restore_skill_source_project_scope()
    _restore_global_skill_identity()


def _restore_skill_source_project_scope() -> None:
    """全 source が一意な Project に写せる場合だけ旧列と制約を復元する。"""

    op.drop_constraint(
        op.f("uq_skill_sources_organization_content_hash"),
        "skill_sources",
        type_="unique",
    )
    op.add_column("skill_sources", sa.Column("project_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE ambiguous bigint;
            BEGIN
                SELECT count(*) INTO ambiguous
                FROM (
                    SELECT sources.id
                    FROM skill_sources sources
                    LEFT JOIN skill_versions versions ON versions.skill_source_id = sources.id
                    LEFT JOIN project_skill_versions bindings
                        ON bindings.skill_version_id = versions.id
                    GROUP BY sources.id
                    HAVING count(DISTINCT bindings.project_id) <> 1
                ) unresolved;
                IF ambiguous > 0 THEN
                    RAISE EXCEPTION
                        'cannot downgrade: % skill source(s) do not map to exactly one project; '
                        'restore the pre-T3 database backup instead', ambiguous;
                END IF;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE skill_sources
            SET project_id = scoped.project_id
            FROM (
                SELECT sources.id source_id, min(bindings.project_id::text)::uuid project_id
                FROM skill_sources sources
                JOIN skill_versions versions ON versions.skill_source_id = sources.id
                JOIN project_skill_versions bindings ON bindings.skill_version_id = versions.id
                GROUP BY sources.id
            ) scoped
            WHERE scoped.source_id = skill_sources.id
            """
        )
    )
    op.alter_column("skill_sources", "project_id", nullable=False)
    op.create_index(op.f("ix_skill_sources_project_id"), "skill_sources", ["project_id"])
    op.create_unique_constraint(
        "uq_skill_sources_project_content_hash",
        "skill_sources",
        ["project_id", "content_hash"],
    )


def _restore_global_skill_identity() -> None:
    """Organization 間で key が重複しない場合だけ旧 global unique key へ戻す。"""

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE conflicts bigint;
            BEGIN
                SELECT count(*) INTO conflicts
                FROM (SELECT key FROM skills GROUP BY key HAVING count(*) > 1) duplicated;
                IF conflicts > 0 THEN
                    RAISE EXCEPTION
                        'cannot downgrade: skills has % key(s) shared by organizations; '
                        'restore the pre-T3 database backup instead', conflicts;
                END IF;
            END $$
            """
        )
    )
    op.drop_constraint(op.f("uq_skills_organization_key"), "skills", type_="unique")
    op.drop_index(op.f("ix_skills_key"), table_name="skills")
    op.create_index(op.f("ix_skills_key"), "skills", ["key"], unique=True)
    op.drop_index(op.f("ix_skills_organization_id"), table_name="skills")
    op.drop_constraint(
        op.f("fk_skills_organization_id_organizations"), "skills", type_="foreignkey"
    )
    op.drop_column("skills", "organization_id")
