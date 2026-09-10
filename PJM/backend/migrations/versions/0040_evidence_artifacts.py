"""Evidence の原 byte を private Artifact として保存し、旧行を補造しない。

Revision ID: 0040_evidence_artifacts
Revises: 0039_document_blob_cleanups
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_evidence_artifacts"
down_revision: str | None = "0039_document_blob_cleanups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """nullable 五列だけを追加し、既存 Evidence/hash/path を書き換えない。"""

    op.add_column("evidence", sa.Column("artifact_ref", sa.String(64), nullable=True))
    op.add_column("evidence", sa.Column("artifact_bytes", sa.LargeBinary(), nullable=True))
    op.add_column("evidence", sa.Column("artifact_size", sa.Integer(), nullable=True))
    op.add_column("evidence", sa.Column("artifact_mime_type", sa.String(255), nullable=True))
    op.add_column("evidence", sa.Column("artifact_path", sa.Text(), nullable=True))
    op.create_index(
        "uq_evidence_artifact_ref", "evidence", ["artifact_ref"], unique=True,
        postgresql_where=sa.text("artifact_ref IS NOT NULL"),
    )
    op.create_check_constraint(
        "artifact_binding", "evidence",
        "(artifact_ref IS NULL AND artifact_bytes IS NULL AND artifact_size IS NULL "
        "AND artifact_mime_type IS NULL AND artifact_path IS NULL) OR "
        "(artifact_ref IS NOT NULL AND artifact_bytes IS NOT NULL "
        "AND artifact_size IS NOT NULL AND artifact_mime_type IS NOT NULL "
        "AND artifact_path IS NOT NULL AND tool_call_id IS NOT NULL "
        "AND artifact_ref ~ '^art_[a-zA-Z0-9_-]+$' "
        "AND evidence_ref ~ '^ev_[a-zA-Z0-9_-]+$' "
        "AND artifact_size BETWEEN 0 AND 1048576 "
        "AND octet_length(artifact_bytes) = artifact_size "
        "AND artifact_mime_type = 'text/plain' "
        "AND content_hash ~ '^sha256:[0-9a-f]{64}$' "
        "AND char_length(artifact_path) BETWEEN 8 AND 4096 "
        "AND artifact_path LIKE 'output/%' AND artifact_path NOT LIKE '%/' "
        "AND artifact_path NOT LIKE '%//%' "
        r"AND artifact_path !~ '(^|/)(\.|\.\.)(/|$)' "
        "AND position(chr(92) in artifact_path) = 0 "
        "AND artifact_path !~ '[[:cntrl:]]')",
    )


def downgrade() -> None:
    """損傷した半 binding を含め、一つでも保存値がある場合は排他 lock 後に拒否する。"""

    op.execute("LOCK TABLE evidence IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM evidence WHERE artifact_ref IS NOT NULL "
        "OR artifact_bytes IS NOT NULL OR artifact_size IS NOT NULL "
        "OR artifact_mime_type IS NOT NULL OR artifact_path IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Artifact records must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_constraint(op.f("ck_evidence_artifact_binding"), "evidence", type_="check")
    op.drop_index("uq_evidence_artifact_ref", table_name="evidence")
    for name in (
        "artifact_path", "artifact_mime_type", "artifact_size", "artifact_bytes", "artifact_ref",
    ):
        op.drop_column("evidence", name)
