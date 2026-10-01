"""画面の同名更新について、旧文書の不変 identity と本文条件を保存する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060_document_upload_replace"
down_revision = "0059_native_resource_clients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧 upload の checksum・公開回执を変更せず、更新の条件だけを追加する。"""
    op.add_column("document_upload_intents", sa.Column("replaces_document_id", sa.Uuid()))
    op.add_column("document_upload_intents", sa.Column("expected_checksum", sa.String(71)))
    op.create_check_constraint(
        op.f("ck_document_upload_intents_replacement"), "document_upload_intents",
        "(replaces_document_id IS NULL AND expected_checksum IS NULL) OR "
        "(replaces_document_id IS NOT NULL AND expected_checksum IS NOT NULL AND "
        "replaces_document_id <> '00000000-0000-0000-0000-000000000000' AND "
        "replaces_document_id <> document_id AND expected_checksum ~ '^sha256:[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    """更新済みの原要求を旧 writer へ渡さず、未使用時にだけ列を戻す。"""
    op.execute("LOCK TABLE document_upload_intents IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_upload_intents "
        "WHERE replaces_document_id IS NOT NULL) THEN RAISE EXCEPTION "
        "'Document replacement history prevents downgrade'; END IF; END $$"
    )
    op.drop_constraint(
        op.f("ck_document_upload_intents_replacement"), "document_upload_intents", type_="check"
    )
    op.drop_column("document_upload_intents", "expected_checksum")
    op.drop_column("document_upload_intents", "replaces_document_id")
