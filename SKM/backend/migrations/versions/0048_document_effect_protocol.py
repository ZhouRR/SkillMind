"""成果台帳に v2 を許可し、v1 の原要求と保存済み回执を変更せずに残す。"""

from __future__ import annotations

from alembic import op

revision = "0048_document_effect_protocol"
down_revision = "0047_effect_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """版の CHECK だけを拡張し、旧 object key・checksum・版は書き換えない。"""

    op.drop_constraint(
        op.f("ck_document_effect_uploads_protocol_version"),
        "document_effect_uploads",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_document_effect_uploads_protocol_version"),
        "document_effect_uploads",
        "protocol_version IN (1, 2)",
    )


def downgrade() -> None:
    """v2 の要求・束縛が一件でもあれば、旧 reader へ降級する前に拒否する。"""

    # 予約前の Run 束縛も保全し、判定後に新しい v2 要求や束縛を作らせない。
    op.execute(
        "LOCK TABLE resource_bindings, document_effect_uploads IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM document_effect_uploads "
        "WHERE protocol_version = 2) OR EXISTS (SELECT 1 FROM resource_bindings "
        "WHERE provider = 'project-library' AND capability_version = 'document.write/v1' "
        "AND revision = '2') THEN "
        "RAISE EXCEPTION 'Document effect protocol v2 records must be preserved before "
        "downgrade'; END IF; END $$;"
    )
    op.drop_constraint(
        op.f("ck_document_effect_uploads_protocol_version"),
        "document_effect_uploads",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_document_effect_uploads_protocol_version"),
        "document_effect_uploads",
        "protocol_version = 1",
    )
