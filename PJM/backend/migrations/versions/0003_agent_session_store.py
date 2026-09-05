"""Claude Agent SDK transcript の PostgreSQL SessionStore table を追加する。

Revision ID: 0003_agent_session_store
Revises: 0002_run_idempotency_outbox
Create Date: 2026-07-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_agent_session_store"
down_revision: str | None = "0002_run_idempotency_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Session key と追加式 transcript entry を保持する table を作成する。"""

    # Key row を lock して sequence 採番を直列化し、複数 Worker 間でも順序を壊さない。
    op.create_table(
        "agent_session_transcripts",
        sa.Column("project_key", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("subpath", sa.String(length=1024), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_session_transcripts")),
        sa.UniqueConstraint(
            "project_key",
            "session_id",
            "subpath",
            name="uq_agent_session_transcripts_key",
        ),
    )
    op.create_index(
        op.f("ix_agent_session_transcripts_project_key"),
        "agent_session_transcripts",
        ["project_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_session_transcripts_session_id"),
        "agent_session_transcripts",
        ["session_id"],
        unique=False,
    )

    # entry_json は SDK 所有の opaque data として解釈せず、checksum と共に追加保存する。
    op.create_table(
        "agent_session_entries",
        sa.Column("transcript_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("entry_uuid", sa.String(length=128), nullable=True),
        sa.Column("entry_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["agent_session_transcripts.id"],
            name=op.f("fk_agent_session_entries_transcript_id_agent_session_transcripts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_session_entries")),
        sa.UniqueConstraint(
            "transcript_id",
            "sequence",
            name="uq_agent_session_entries_sequence",
        ),
    )
    op.create_index(
        op.f("ix_agent_session_entries_transcript_id"),
        "agent_session_entries",
        ["transcript_id"],
        unique=False,
    )
    op.create_index(
        "uq_agent_session_entries_transcript_uuid",
        "agent_session_entries",
        ["transcript_id", "entry_uuid"],
        unique=True,
        postgresql_where=sa.text("entry_uuid IS NOT NULL"),
    )


def downgrade() -> None:
    """Transcript entry と key table を依存関係の逆順で削除する。"""

    op.drop_index(
        "uq_agent_session_entries_transcript_uuid",
        table_name="agent_session_entries",
    )
    op.drop_index(
        op.f("ix_agent_session_entries_transcript_id"),
        table_name="agent_session_entries",
    )
    op.drop_table("agent_session_entries")
    op.drop_index(
        op.f("ix_agent_session_transcripts_session_id"),
        table_name="agent_session_transcripts",
    )
    op.drop_index(
        op.f("ix_agent_session_transcripts_project_key"),
        table_name="agent_session_transcripts",
    )
    op.drop_table("agent_session_transcripts")
