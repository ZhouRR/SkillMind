"""RunResult を通用 OutcomeEnvelope と任意業務 Schema の保存形へ拡張する。

Revision ID: 0019_outcome_envelope
Revises: 0018_skill_library_scope
Create Date: 2026-07-18

docs/01 §15 S3。既存 Result は歴史的 STRUCTURED_OUTPUT として保持し、新規 Run は
OUTCOME_ENVELOPE を保存する。元 data_json を書き換えず、参照と任意 Schema identity を
追加列へ投影することで監査資産の不変性を守る。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_outcome_envelope"
down_revision: str | None = "0018_skill_library_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """既存行へ歴史的 kind と空の参照 metadata を補完する。"""

    op.add_column(
        "run_results",
        sa.Column(
            "result_kind",
            sa.String(length=32),
            nullable=False,
            server_default="STRUCTURED_OUTPUT",
        ),
    )
    op.add_column(
        "run_results",
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "run_results",
        sa.Column("artifact_refs_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "run_results",
        sa.Column(
            "optional_schema_identity_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column("run_results", "result_kind", server_default=None)
    op.alter_column("run_results", "evidence_refs_json", server_default=None)
    op.alter_column("run_results", "artifact_refs_json", server_default=None)
    op.alter_column("run_results", "optional_schema_identity_json", server_default=None)


def downgrade() -> None:
    """Outcome 固有 metadata を削除し、歴史的 Result 保存形へ戻す。"""

    op.drop_column("run_results", "optional_schema_identity_json")
    op.drop_column("run_results", "artifact_refs_json")
    op.drop_column("run_results", "evidence_refs_json")
    op.drop_column("run_results", "result_kind")
