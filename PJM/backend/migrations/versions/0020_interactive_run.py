"""RunSegment、構造化 Interaction と順次 Session lineage を追加する。

Revision ID: 0020_interactive_run
Revises: 0019_outcome_envelope
Create Date: 2026-07-18

docs/01 §15 S4。既存 Run を業務上の推測で backfill せず、nullable foreign key と read-side
implicit Segment 1 で保持する。Release G 以降の新規 Run だけが明示 Segment、Brief snapshot、
Interaction と continuation metadata を作成する。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_interactive_run"
down_revision: str | None = "0019_outcome_envelope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新規 Run 用の Segment/Interaction/Session 監査 schema を追加する。"""

    op.create_table(
        "run_segments",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("segment_no", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("trigger_ref", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("objective_json", sa.JSON(), nullable=False),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False),
        sa.Column("continuation_mode", sa.String(length=16), nullable=False),
        sa.Column("parent_agent_session_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_agent_session_id"],
            ["agent_sessions.id"],
            name=op.f("fk_run_segments_parent_agent_session_id_agent_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_segments_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_segments")),
        sa.UniqueConstraint("run_id", "segment_no", name="uq_run_segments_run_no"),
    )
    op.create_index(op.f("ix_run_segments_run_id"), "run_segments", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_run_segments_trigger_ref"), "run_segments", ["trigger_ref"], unique=False
    )
    op.create_index(
        op.f("ix_run_segments_parent_agent_session_id"),
        "run_segments",
        ["parent_agent_session_id"],
        unique=False,
    )

    op.create_table(
        "agent_task_brief_snapshots",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_segment_id", sa.Uuid(), nullable=False),
        sa.Column("brief_version", sa.String(length=64), nullable=False),
        sa.Column("brief_json", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(length=71), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_agent_task_brief_snapshots_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_segment_id"],
            ["run_segments.id"],
            name=op.f("fk_agent_task_brief_snapshots_run_segment_id_run_segments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_task_brief_snapshots")),
        sa.UniqueConstraint(
            "run_segment_id", name=op.f("uq_agent_task_brief_snapshots_run_segment_id")
        ),
    )
    op.create_index(
        op.f("ix_agent_task_brief_snapshots_run_id"),
        "agent_task_brief_snapshots",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_task_brief_snapshots_run_segment_id"),
        "agent_task_brief_snapshots",
        ["run_segment_id"],
        unique=True,
    )
    op.add_column("run_segments", sa.Column("instruction_snapshot_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_run_segments_instruction_snapshot_id_agent_task_brief_snapshots"),
        "run_segments",
        "agent_task_brief_snapshots",
        ["instruction_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_run_segments_instruction_snapshot_id"),
        "run_segments",
        ["instruction_snapshot_id"],
    )

    op.add_column("run_attempts", sa.Column("run_segment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_run_attempts_run_segment_id_run_segments"),
        "run_attempts",
        "run_segments",
        ["run_segment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_run_attempts_run_segment_id"),
        "run_attempts",
        ["run_segment_id"],
        unique=False,
    )
    op.drop_constraint("uq_run_attempts_run_id", "run_attempts", type_="unique")
    op.create_unique_constraint(
        "uq_run_attempts_segment_attempt",
        "run_attempts",
        ["run_segment_id", "attempt_no"],
    )

    op.add_column("agent_sessions", sa.Column("run_segment_id", sa.Uuid(), nullable=True))
    op.add_column(
        "agent_sessions",
        sa.Column(
            "continuation_mode",
            sa.String(length=16),
            nullable=False,
            server_default="INITIAL",
        ),
    )
    op.add_column(
        "agent_sessions", sa.Column("checkpoint_checksum", sa.String(length=71), nullable=True)
    )
    op.add_column(
        "agent_sessions",
        sa.Column("engine_options_checksum", sa.String(length=71), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_agent_sessions_run_segment_id_run_segments"),
        "agent_sessions",
        "run_segments",
        ["run_segment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_agent_sessions_parent_session_id_agent_sessions"),
        "agent_sessions",
        "agent_sessions",
        ["parent_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_agent_sessions_run_segment_id"),
        "agent_sessions",
        ["run_segment_id"],
        unique=False,
    )
    op.create_index(
        "uq_agent_sessions_active_run",
        "agent_sessions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.alter_column("agent_sessions", "continuation_mode", server_default=None)

    op.create_table(
        "user_interactions",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_segment_id", sa.Uuid(), nullable=False),
        sa.Column("agent_session_id", sa.Uuid(), nullable=False),
        sa.Column("interaction_type", sa.String(length=32), nullable=False),
        sa.Column("prompt_json", sa.JSON(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("continuation_mode", sa.String(length=16), nullable=False),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False),
        sa.Column("checkpoint_checksum", sa.String(length=71), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_session_id"],
            ["agent_sessions.id"],
            name=op.f("fk_user_interactions_agent_session_id_agent_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_user_interactions_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_segment_id"],
            ["run_segments.id"],
            name=op.f("fk_user_interactions_run_segment_id_run_segments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_interactions")),
    )
    for column in ("run_id", "run_segment_id", "agent_session_id", "expires_at"):
        op.create_index(
            op.f(f"ix_user_interactions_{column}"),
            "user_interactions",
            [column],
            unique=False,
        )
    op.create_index(
        "uq_user_interactions_open_required_run",
        "user_interactions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN' AND required = true"),
    )

    op.create_table(
        "interaction_responses",
        sa.Column("interaction_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("interaction_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["interaction_id"],
            ["user_interactions.id"],
            name=op.f("fk_interaction_responses_interaction_id_user_interactions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_interaction_responses_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interaction_responses")),
        sa.UniqueConstraint(
            "interaction_id", name="uq_interaction_responses_interaction"
        ),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_interaction_responses_run_idempotency"
        ),
    )
    for column in ("interaction_id", "run_id", "actor_id"):
        op.create_index(
            op.f(f"ix_interaction_responses_{column}"),
            "interaction_responses",
            [column],
            unique=False,
        )


def downgrade() -> None:
    """Release G の追加式 schema を除去し、単一 Segment の保存形へ戻す。"""

    for column in ("actor_id", "run_id", "interaction_id"):
        op.drop_index(
            op.f(f"ix_interaction_responses_{column}"), table_name="interaction_responses"
        )
    op.drop_table("interaction_responses")

    op.drop_index(
        "uq_user_interactions_open_required_run", table_name="user_interactions"
    )
    for column in ("expires_at", "agent_session_id", "run_segment_id", "run_id"):
        op.drop_index(op.f(f"ix_user_interactions_{column}"), table_name="user_interactions")
    op.drop_table("user_interactions")

    op.drop_index("uq_agent_sessions_active_run", table_name="agent_sessions")
    op.drop_index(op.f("ix_agent_sessions_run_segment_id"), table_name="agent_sessions")
    op.drop_constraint(
        op.f("fk_agent_sessions_parent_session_id_agent_sessions"),
        "agent_sessions",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_agent_sessions_run_segment_id_run_segments"),
        "agent_sessions",
        type_="foreignkey",
    )
    op.drop_column("agent_sessions", "engine_options_checksum")
    op.drop_column("agent_sessions", "checkpoint_checksum")
    op.drop_column("agent_sessions", "continuation_mode")
    op.drop_column("agent_sessions", "run_segment_id")

    op.drop_constraint(
        "uq_run_attempts_segment_attempt", "run_attempts", type_="unique"
    )
    op.create_unique_constraint(
        "uq_run_attempts_run_id", "run_attempts", ["run_id", "attempt_no"]
    )
    op.drop_index(op.f("ix_run_attempts_run_segment_id"), table_name="run_attempts")
    op.drop_constraint(
        op.f("fk_run_attempts_run_segment_id_run_segments"),
        "run_attempts",
        type_="foreignkey",
    )
    op.drop_column("run_attempts", "run_segment_id")

    op.drop_constraint(
        op.f("uq_run_segments_instruction_snapshot_id"),
        "run_segments",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_run_segments_instruction_snapshot_id_agent_task_brief_snapshots"),
        "run_segments",
        type_="foreignkey",
    )
    op.drop_column("run_segments", "instruction_snapshot_id")
    op.drop_index(
        op.f("ix_agent_task_brief_snapshots_run_segment_id"),
        table_name="agent_task_brief_snapshots",
    )
    op.drop_index(
        op.f("ix_agent_task_brief_snapshots_run_id"),
        table_name="agent_task_brief_snapshots",
    )
    op.drop_table("agent_task_brief_snapshots")

    op.drop_index(
        op.f("ix_run_segments_parent_agent_session_id"), table_name="run_segments"
    )
    op.drop_index(op.f("ix_run_segments_trigger_ref"), table_name="run_segments")
    op.drop_index(op.f("ix_run_segments_run_id"), table_name="run_segments")
    op.drop_table("run_segments")
