"""生成 FrontendModule の凍結版を保持する frontend_module_versions table を追加する。

Revision ID: 0026_frontend_module_versions
Revises: 0025_task_schedules
Create Date: 2026-07-26

docs/01 §24 M1 / docs/07 §10。source/lockfile/bundle の hash、Module API version、配信時 CSP、
静的検査と build の報告を一行に凍結し、精確 SkillVersion へ束縛する。本 migration は table だけを
足し、生成コードの実行経路は一切追加しない (§24.3 の M1 は M3 の脅威 model 準入条件に依存しない)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_frontend_module_versions"
down_revision: str | None = "0025_task_schedules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """frontend_module_versions table を作成する。"""

    op.create_table(
        "frontend_module_versions",
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("module_api_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_hash", sa.String(length=71), nullable=False),
        sa.Column("lockfile_hash", sa.String(length=71), nullable=True),
        sa.Column("bundle_hash", sa.String(length=71), nullable=True),
        sa.Column("content_security_policy", sa.String(length=512), nullable=False),
        sa.Column("static_report_json", sa.JSON(), nullable=False),
        sa.Column("build_report_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(length=256), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'BUILT', 'PUBLISHED', 'DISABLED')",
            name=op.f("ck_frontend_module_versions_status"),
        ),
        # 配信できない版を「公開済み」と呼ばない。
        sa.CheckConstraint(
            "status IN ('DRAFT', 'DISABLED') OR bundle_hash IS NOT NULL",
            name=op.f("ck_frontend_module_versions_bundle_required"),
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name=op.f("fk_frontend_module_versions_skill_version_id_skill_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_frontend_module_versions_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_frontend_module_versions")),
        sa.UniqueConstraint(
            "skill_version_id",
            "source_hash",
            name="uq_frontend_module_versions_skill_source",
        ),
    )
    op.create_index(
        op.f("ix_frontend_module_versions_skill_version_id"),
        "frontend_module_versions",
        ["skill_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_frontend_module_versions_created_by"),
        "frontend_module_versions",
        ["created_by"],
        unique=False,
    )


def downgrade() -> None:
    """frontend_module_versions table を削除する。"""

    op.drop_index(
        op.f("ix_frontend_module_versions_created_by"), table_name="frontend_module_versions"
    )
    op.drop_index(
        op.f("ix_frontend_module_versions_skill_version_id"),
        table_name="frontend_module_versions",
    )
    op.drop_table("frontend_module_versions")
