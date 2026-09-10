"""Project 文書 metadata table を追加する。

Revision ID: 0014_project_documents
Revises: 0013_skill_lineage
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_project_documents"
down_revision: str | None = "0013_skill_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """文書 metadata の正本 table を作成する。blob 正文は object storage に置く。"""

    op.create_table(
        "project_documents",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("folder", sa.String(200), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("checksum", sa.String(71), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_documents")),
        # 同一 folder 内の name 重複を拒否し、一覧の一意性を保証する。
        sa.UniqueConstraint(
            "project_id",
            "folder",
            "name",
            name="uq_project_documents_project_folder_name",
        ),
    )
    op.create_index(
        op.f("ix_project_documents_project_id"),
        "project_documents",
        ["project_id"],
    )
    op.create_index(
        op.f("ix_project_documents_uploaded_by"),
        "project_documents",
        ["uploaded_by"],
    )


def downgrade() -> None:
    """文書 metadata table を削除する。"""

    op.drop_index(op.f("ix_project_documents_uploaded_by"), table_name="project_documents")
    op.drop_index(op.f("ix_project_documents_project_id"), table_name="project_documents")
    op.drop_table("project_documents")
