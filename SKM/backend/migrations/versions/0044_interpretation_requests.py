"""非同期解釈の原要求を追加し、旧結果や Queue に開始資格を補造しない。

Revision ID: 0044_interpretation_requests
Revises: 0043_budget_start_owner
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_interpretation_requests"
down_revision: str | None = "0043_budget_start_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空の要求/呼出し台帳だけを作り、旧解釈と Outbox は変更しない。"""

    op.create_table(
        "skill_interpretation_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "auth_session_id",
            sa.Uuid(),
            sa.ForeignKey("auth_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("accepted_http_request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "skill_source_id",
            sa.Uuid(),
            sa.ForeignKey("skill_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("execution_key", sa.String(71), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("input_checksum", sa.String(71), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner_hash", sa.String(71)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "interpretation_id",
            sa.Uuid(),
            sa.ForeignKey("skill_interpretations.id", ondelete="RESTRICT"),
        ),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint(
            "organization_id", "execution_key", name="uq_interpret_request_execution"
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'UNKNOWN', 'SUCCEEDED', 'FAILED', 'REVOKED')",
            name="interpret_request_status",
        ),
        sa.CheckConstraint(
            "input_checksum ~ '^sha256:[0-9a-f]{64}$' AND execution_key ~ '^sha256:[0-9a-f]{64}$'",
            name="interpret_request_hashes",
        ),
        sa.CheckConstraint(
            "(owner_hash IS NULL AND claimed_at IS NULL) OR "
            "(owner_hash IS NOT NULL AND owner_hash ~ '^sha256:[0-9a-f]{64}$' "
            "AND claimed_at IS NOT NULL)",
            name="interpret_request_owner",
        ),
        sa.CheckConstraint(
            "(status = 'QUEUED' AND owner_hash IS NULL AND finished_at IS NULL "
            "AND interpretation_id IS NULL AND error_code IS NULL) OR "
            "(status IN ('RUNNING', 'UNKNOWN') AND owner_hash IS NOT NULL "
            "AND finished_at IS NULL AND interpretation_id IS NULL AND error_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND finished_at IS NOT NULL "
            "AND interpretation_id IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('FAILED', 'REVOKED') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="interpret_request_terminal",
        ),
    )
    op.create_index(
        "ix_skill_interpretation_requests_organization_id",
        "skill_interpretation_requests",
        ["organization_id"],
    )
    op.create_index(
        "ix_skill_interpretation_requests_execution_key",
        "skill_interpretation_requests",
        ["execution_key"],
    )
    op.create_table(
        "skill_interpretation_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("skill_interpretation_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("feedback", sa.Text()),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("returned_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("request_id", "ordinal", name="uq_interpret_call_request_ordinal"),
        sa.CheckConstraint(
            "(ordinal = 0 AND feedback IS NULL) OR (ordinal = 1 AND feedback IS NOT NULL)",
            name="interpret_call_ordinal",
        ),
        sa.CheckConstraint(
            "returned_at IS NULL OR returned_at >= granted_at", name="interpret_call_return"
        ),
    )


def downgrade() -> None:
    """原要求が一つでも存在すれば、監査を失う降級を中断する。"""

    op.execute(
        "LOCK TABLE skill_interpretation_requests, skill_interpretation_calls "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM skill_interpretation_requests) OR "
        "EXISTS (SELECT 1 FROM skill_interpretation_calls) THEN RAISE EXCEPTION "
        "'Interpretation requests must be preserved before downgrade'; END IF; END $$;"
    )
    op.drop_table("skill_interpretation_calls")
    op.drop_table("skill_interpretation_requests")
