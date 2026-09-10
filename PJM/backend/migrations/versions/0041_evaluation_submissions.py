"""Evaluation の原要求を保存し、旧評価へキーを補造しない。

Revision ID: 0041_evaluation_submissions
Revises: 0040_evidence_artifacts
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_evaluation_submissions"
down_revision: str | None = "0040_evidence_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """原キー/hash を nullable で追加し、同一 Result/actor/key を一意に保つ。"""

    op.add_column("evaluations", sa.Column("submission_key", sa.Uuid(), nullable=True))
    op.add_column("evaluations", sa.Column("request_hash", sa.String(71), nullable=True))
    op.create_check_constraint(
        "submission_binding",
        "evaluations",
        "(submission_key IS NULL AND request_hash IS NULL) OR "
        "(submission_key IS NOT NULL AND request_hash IS NOT NULL "
        "AND submission_key <> '00000000-0000-0000-0000-000000000000' "
        "AND request_hash ~ '^sha256:[0-9a-f]{64}$')",
    )
    op.create_unique_constraint(
        "uq_evaluations_submission",
        "evaluations",
        ["result_id", "user_id", "submission_key"],
    )
    op.create_index(
        "ix_evaluations_result_created_id",
        "evaluations",
        ["result_id", "created_at", "id"],
    )


def downgrade() -> None:
    """半 binding を含む原要求があれば、排他 lock の後に降級を中断する。"""

    op.execute("LOCK TABLE evaluations IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM evaluations WHERE submission_key IS NOT NULL "
        "OR request_hash IS NOT NULL) THEN "
        "RAISE EXCEPTION 'Evaluation submissions must be preserved before downgrade'; "
        "END IF; END $$;"
    )
    op.drop_index("ix_evaluations_result_created_id", table_name="evaluations")
    op.drop_constraint("uq_evaluations_submission", "evaluations", type_="unique")
    op.drop_constraint(op.f("ck_evaluations_submission_binding"), "evaluations", type_="check")
    op.drop_column("evaluations", "request_hash")
    op.drop_column("evaluations", "submission_key")
