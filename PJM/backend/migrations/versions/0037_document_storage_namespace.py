"""文書の保存先 identity を保持し、旧 key から現在の namespace を推定しない。

Revision ID: 0037_document_storage_namespace
Revises: 0036_schedule_occurrences
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_document_storage_namespace"
down_revision: str | None = "0036_schedule_occurrences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """三列と CHECK だけを追加し、旧 ID/key/hash や未確定の保存先を書き換えない。"""

    op.add_column(
        "project_documents", sa.Column("storage_namespace_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "project_documents", sa.Column("storage_descriptor_checksum", sa.String(71), nullable=True)
    )
    op.add_column(
        "project_documents", sa.Column("storage_is_durable", sa.Boolean(), nullable=True)
    )
    op.create_check_constraint(
        "storage_namespace_binding",
        "project_documents",
        "(storage_namespace_id IS NULL AND storage_descriptor_checksum IS NULL "
        "AND storage_is_durable IS NULL) OR "
        "(storage_namespace_id IS NOT NULL AND storage_descriptor_checksum IS NOT NULL "
        "AND storage_is_durable IS NOT NULL "
        "AND storage_namespace_id <> '00000000-0000-0000-0000-000000000000' "
        "AND storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    """未関連行だけのときに限り、排他 lock 後の server 判定で帰属列の破棄を許可する。"""

    # offline SQL にも同じ guard を出力し、空判定から DROP の間の新しい保存を止める。
    op.execute("LOCK TABLE project_documents IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM project_documents WHERE "
        "storage_namespace_id IS NOT NULL OR storage_descriptor_checksum IS NOT NULL "
        "OR storage_is_durable IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Document storage namespace bindings must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint(
        op.f("ck_project_documents_storage_namespace_binding"), "project_documents", type_="check"
    )
    op.drop_column("project_documents", "storage_is_durable")
    op.drop_column("project_documents", "storage_descriptor_checksum")
    op.drop_column("project_documents", "storage_namespace_id")
