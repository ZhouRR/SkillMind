"""原 Effect の只読核対を、原会話・単一開始・観測に分離して保存する。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_effect_reconciliation"
down_revision = "0046_document_library_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """空の核対台帳だけを作り、旧 Effect/Run を変更しない。"""

    op.create_table(
        "effect_reconciliation_requests",
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
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "effect_execution_id",
            sa.Uuid(),
            sa.ForeignKey("effect_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_checksum", sa.String(71), nullable=False),
        sa.Column("command_checksum", sa.String(71), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner_hash", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("observation_status", sa.String(16)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("receipt_json", sa.JSON(none_as_null=True)),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REVOKED')",
            name="effect_reconciliation_status",
        ),
        sa.CheckConstraint(
            "kind IN ('DATABASE_TRANSACTION', 'DOCUMENT_OBJECT')", name="effect_reconciliation_kind"
        ),
        sa.CheckConstraint(
            "target_checksum ~ '^sha256:[0-9a-f]{64}$' "
            "AND command_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name="effect_reconciliation_hashes",
        ),
        sa.CheckConstraint(
            "(owner_hash IS NULL AND claimed_at IS NULL AND lease_expires_at IS NULL) OR "
            "(owner_hash IS NOT NULL AND owner_hash ~ '^[0-9a-f]{64}$' "
            "AND claimed_at IS NOT NULL AND claimed_at >= created_at "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > claimed_at)",
            name="effect_reconciliation_owner",
        ),
        sa.CheckConstraint(
            "(status = 'QUEUED' AND owner_hash IS NULL AND finished_at IS NULL "
            "AND observation_status IS NULL AND observed_at IS NULL "
            "AND receipt_json IS NULL AND error_code IS NULL) OR "
            "(status = 'RUNNING' AND owner_hash IS NOT NULL AND finished_at IS NULL "
            "AND observation_status IS NULL AND observed_at IS NULL "
            "AND receipt_json IS NULL AND error_code IS NULL) OR "
            "(status = 'SUCCEEDED' AND owner_hash IS NOT NULL AND finished_at IS NOT NULL "
            "AND finished_at >= observed_at AND finished_at < lease_expires_at "
            "AND observed_at IS NOT NULL AND observed_at >= claimed_at AND error_code IS NULL "
            "AND observation_status IS NOT NULL "
            "AND ((observation_status = 'CONFIRMED' AND receipt_json IS NOT NULL) OR "
            "(observation_status IN ('NOT_OBSERVED', 'CONFLICT') AND receipt_json IS NULL))) OR "
            "(status IN ('FAILED', 'REVOKED') AND finished_at IS NOT NULL "
            "AND finished_at >= created_at AND observation_status IS NULL "
            "AND observed_at IS NULL AND receipt_json IS NULL AND error_code IS NOT NULL)",
            name="effect_reconciliation_state",
        ),
    )
    op.create_index(
        "uq_effect_reconciliation_active",
        "effect_reconciliation_requests",
        ["effect_execution_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    op.create_index(
        "ix_effect_reconciliation_requests_lease_expires_at",
        "effect_reconciliation_requests",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    """終態を含む原核対記録があれば、監査を消す降級を拒否する。"""

    op.execute("LOCK TABLE effect_reconciliation_requests IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM effect_reconciliation_requests) THEN
            RAISE EXCEPTION 'Effect reconciliation requests prevent downgrade';
          END IF;
        END $$;
    """)
    op.drop_table("effect_reconciliation_requests")
