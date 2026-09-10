"""ToolCall、PermissionDecision、Evidence table を追加する。

Revision ID: 0004_tool_audit_evidence
Revises: 0003_agent_session_store
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_tool_audit_evidence"
down_revision: str | None = "0003_agent_session_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Tool 実行と Evidence を追加式に監査する table を作成する。"""

    op.create_table(
        "tool_calls",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("agent_session_id", sa.Uuid(), nullable=False),
        sa.Column("sdk_tool_use_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("arguments_summary", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_tool_calls_run_attempt_id_run_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_tool_calls_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
        sa.UniqueConstraint("run_id", "sdk_tool_use_id", name="uq_tool_calls_run_sdk_use"),
    )
    for column in ("run_id", "run_attempt_id", "agent_session_id", "integration_id"):
        op.create_index(op.f(f"ix_tool_calls_{column}"), "tool_calls", [column], unique=False)

    op.create_table(
        "permission_decisions",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_permission_decisions_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_calls.id"],
            name=op.f("fk_permission_decisions_tool_call_id_tool_calls"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permission_decisions")),
        sa.UniqueConstraint(
            "tool_call_id",
            "decision",
            "request_fingerprint",
            name="uq_permission_decisions_call_decision_request",
        ),
    )
    for column in ("run_id", "tool_call_id", "decided_by"):
        op.create_index(
            op.f(f"ix_permission_decisions_{column}"),
            "permission_decisions",
            [column],
            unique=False,
        )

    op.create_table(
        "evidence",
        sa.Column("evidence_ref", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=2048), nullable=False),
        sa.Column("source_locator", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=71), nullable=False),
        sa.Column("snapshot_uri", sa.String(length=2048), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_evidence_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_calls.id"],
            name=op.f("fk_evidence_tool_call_id_tool_calls"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.UniqueConstraint("evidence_ref", name=op.f("uq_evidence_evidence_ref")),
    )
    for column in ("evidence_ref", "run_id", "tool_call_id"):
        op.create_index(op.f(f"ix_evidence_{column}"), "evidence", [column], unique=False)


def downgrade() -> None:
    """Evidence、PermissionDecision、ToolCall を依存関係の逆順で削除する。"""

    for column in ("tool_call_id", "run_id", "evidence_ref"):
        op.drop_index(op.f(f"ix_evidence_{column}"), table_name="evidence")
    op.drop_table("evidence")
    for column in ("decided_by", "tool_call_id", "run_id"):
        op.drop_index(
            op.f(f"ix_permission_decisions_{column}"),
            table_name="permission_decisions",
        )
    op.drop_table("permission_decisions")
    for column in ("integration_id", "agent_session_id", "run_attempt_id", "run_id"):
        op.drop_index(op.f(f"ix_tool_calls_{column}"), table_name="tool_calls")
    op.drop_table("tool_calls")
