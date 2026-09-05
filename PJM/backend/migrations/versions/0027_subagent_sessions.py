"""扇出の子 AgentSession を保存できるよう、二つの一意制約を PRIMARY 限定へ収縮する。

Revision ID: 0027_subagent_sessions
Revises: 0026_frontend_module_versions
Create Date: 2026-07-26

docs/01 §23 P3b / docs/06 §3。`subagent.dispatch/v1` の各 branch は主 Session と**同じ
RunAttempt の下**で**同時に**走る。既存の二制約はどちらも「Run 骨格の本体は一本」という不変条件を
守るものだが、対象を絞っていないため子を保存した瞬間に違反する:

- `uq_agent_sessions_run_attempt` (Attempt ごと一つ) → 子は親と同じ Attempt を共有する
- `uq_agent_sessions_active_run` (Run に ACTIVE 一つ) → 子は最大 4 本が同時 ACTIVE になる

**制約を外すのではなく、PRIMARY 行だけへ掛け直す。** 外すと「Attempt に本体 session が二つ」も
通ってしまい、守っていた不変条件そのものが消える。session_kind は CHECK で二値に固定し、
未知の値で判定を素通りできないようにする。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_subagent_sessions"
down_revision: str | None = "0026_frontend_module_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """二つの一意制約を PRIMARY 限定にし、session_kind と continuation_mode を固定する。"""

    # table 制約は部分 index にできないため、一意 index へ置き換える。
    op.drop_constraint("uq_agent_sessions_run_attempt", "agent_sessions", type_="unique")
    op.create_index(
        "uq_agent_sessions_primary_run_attempt",
        "agent_sessions",
        ["run_attempt_id"],
        unique=True,
        postgresql_where=sa.text("session_kind = 'PRIMARY'"),
    )
    op.drop_index("uq_agent_sessions_active_run", table_name="agent_sessions")
    op.create_index(
        "uq_agent_sessions_active_run",
        "agent_sessions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND session_kind = 'PRIMARY'"),
    )
    op.create_index(
        "ix_agent_sessions_parent_session_id_kind",
        "agent_sessions",
        ["parent_session_id", "session_kind"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_agent_sessions_session_kind",
        "agent_sessions",
        "session_kind IN ('PRIMARY', 'SUBAGENT')",
    )
    # SDK が session を開く前に落ちた branch は、実在する SDK session を持たない。列を NOT NULL の
    # まま残すと**存在しない ID を捏造する**しかなくなり、「引けるように見えて引けない参照」という
    # 本切片が潰そうとしている不具合をそのまま作り直すことになる。PRIMARY 側の NOT NULL は
    # CHECK で維持する。
    op.alter_column("agent_sessions", "sdk_session_id", nullable=True)
    op.create_check_constraint(
        "ck_agent_sessions_primary_has_sdk_session",
        "agent_sessions",
        "sdk_session_id IS NOT NULL OR session_kind = 'SUBAGENT'",
    )
    # BRANCH は扇出の一路だけが取る。PRIMARY が BRANCH を名乗ると、監査で「同じ Attempt に
    # session が複数ある」理由が読めなくなるので、対応関係を DB 側で固定する。
    op.create_check_constraint(
        "ck_agent_sessions_branch_is_subagent",
        "agent_sessions",
        "(continuation_mode = 'BRANCH') = (session_kind = 'SUBAGENT')",
    )


def downgrade() -> None:
    """PRIMARY 限定の制約を元の全行制約へ戻す。"""

    op.drop_constraint("ck_agent_sessions_branch_is_subagent", "agent_sessions", type_="check")
    op.execute("DELETE FROM agent_sessions WHERE sdk_session_id IS NULL")
    op.drop_constraint(
        "ck_agent_sessions_primary_has_sdk_session", "agent_sessions", type_="check"
    )
    op.alter_column("agent_sessions", "sdk_session_id", nullable=False)
    op.drop_constraint("ck_agent_sessions_session_kind", "agent_sessions", type_="check")
    op.drop_index("ix_agent_sessions_parent_session_id_kind", table_name="agent_sessions")
    op.drop_index("uq_agent_sessions_active_run", table_name="agent_sessions")
    op.create_index(
        "uq_agent_sessions_active_run",
        "agent_sessions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.drop_index("uq_agent_sessions_primary_run_attempt", table_name="agent_sessions")
    op.create_unique_constraint(
        "uq_agent_sessions_run_attempt", "agent_sessions", ["run_attempt_id"]
    )
