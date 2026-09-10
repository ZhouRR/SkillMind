"""AgentSession、RunResult、RunEvent session identity を追加する。

Revision ID: 0005_agent_result_terminal
Revises: 0004_tool_audit_evidence
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_agent_result_terminal"
down_revision: str | None = "0004_tool_audit_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Session metadata と immutable Result table を作成する。"""

    op.add_column("run_events", sa.Column("agent_session_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_run_events_agent_session_id"),
        "run_events",
        ["agent_session_id"],
        unique=False,
    )

    op.create_table(
        "agent_sessions",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("sdk_session_id", sa.Uuid(), nullable=False),
        sa.Column("parent_session_id", sa.Uuid(), nullable=True),
        sa.Column("engine", sa.String(length=64), nullable=False),
        sa.Column("session_kind", sa.String(length=32), nullable=False),
        sa.Column("cwd", sa.String(length=4096), nullable=False),
        sa.Column("sdk_version", sa.String(length=32), nullable=False),
        sa.Column("cli_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("cost_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_agent_sessions_run_attempt_id_run_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_agent_sessions_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_sessions")),
        sa.UniqueConstraint("run_attempt_id", name="uq_agent_sessions_run_attempt"),
    )
    for column in ("run_id", "run_attempt_id", "sdk_session_id", "parent_session_id"):
        op.create_index(
            op.f(f"ix_agent_sessions_{column}"),
            "agent_sessions",
            [column],
            unique=False,
        )

    op.create_table(
        "run_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_session_id", sa.Uuid(), nullable=False),
        sa.Column("output_schema", sa.String(length=512), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("needs_review", sa.Boolean(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("cost_json", sa.JSON(), nullable=False),
        sa.Column("validation_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_results_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_results")),
    )
    op.create_index(op.f("ix_run_results_run_id"), "run_results", ["run_id"], unique=True)
    op.create_index(
        op.f("ix_run_results_agent_session_id"),
        "run_results",
        ["agent_session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Result、Session、RunEvent column を依存関係の逆順で削除する。"""

    op.drop_index(op.f("ix_run_results_agent_session_id"), table_name="run_results")
    op.drop_index(op.f("ix_run_results_run_id"), table_name="run_results")
    op.drop_table("run_results")
    for column in ("parent_session_id", "sdk_session_id", "run_attempt_id", "run_id"):
        op.drop_index(op.f(f"ix_agent_sessions_{column}"), table_name="agent_sessions")
    op.drop_table("agent_sessions")
    op.drop_index(op.f("ix_run_events_agent_session_id"), table_name="run_events")
    op.drop_column("run_events", "agent_session_id")
