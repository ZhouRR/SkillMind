"""認証 identity、session、Project membership の基礎 table を追加する。

Revision ID: 0010_identity_and_projects
Revises: 0009_run_skill_snapshots
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0010_identity_and_projects"
down_revision: str | None = "0009_run_skill_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ORGANIZATION_ID = UUID("00000000-0000-4000-8000-000000000100")


def upgrade() -> None:
    """Identity table と初期 ADMIN 用の単一 Organization を作成する。"""

    _create_organizations()
    _create_users()
    _create_projects()
    _create_project_members()
    _create_auth_sessions()
    _initialize_system_organization()


def downgrade() -> None:
    """Identity table を依存関係の逆順で削除する。"""

    op.drop_table("auth_sessions")
    op.drop_table("project_members")
    op.drop_table("projects")
    op.drop_table("users")
    op.drop_table("organizations")


def _create_organizations() -> None:
    """単一 Organization の policy table を作成する。"""

    op.create_table(
        "organizations",
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("default_retention_days", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )


def _create_users() -> None:
    """User credential と system role table を作成する。"""

    op.create_table(
        "users",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("system_role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "system_role IN ('ADMIN', 'USER')",
            name=op.f("ck_users_users_system_role"),
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name=op.f("ck_users_users_status")),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("organization_id", "email", name="uq_users_organization_email"),
    )
    op.create_index(op.f("ix_users_organization_id"), "users", ["organization_id"])


def _create_projects() -> None:
    """Project isolation boundary table を作成する。"""

    op.create_table(
        "projects",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ARCHIVED')",
            name=op.f("ck_projects_projects_status"),
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("organization_id", "key", name="uq_projects_organization_key"),
    )
    op.create_index(op.f("ix_projects_organization_id"), "projects", ["organization_id"])


def _create_project_members() -> None:
    """Project membership table を作成する。"""

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REMOVED')",
            name=op.f("ck_project_members_project_members_status"),
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_members")),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
    )
    op.create_index(op.f("ix_project_members_project_id"), "project_members", ["project_id"])
    op.create_index(op.f("ix_project_members_user_id"), "project_members", ["user_id"])


def _create_auth_sessions() -> None:
    """Plain token を保存しない browser session table を作成する。"""

    op.create_table(
        "auth_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(71), nullable=False),
        sa.Column("csrf_token_hash", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_ip_hash", sa.String(71), nullable=True),
        sa.Column("user_agent_hash", sa.String(71), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    op.create_index(
        op.f("ix_auth_sessions_token_hash"),
        "auth_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"])


def _initialize_system_organization() -> None:
    """初期 ADMIN の排他 lock と所属先になる Organization だけを初期化する。"""

    op.execute(
        sa.text(
            f"""INSERT INTO organizations
              (id, name, status, settings_json, default_retention_days, created_at, updated_at)
            VALUES
              ('{SYSTEM_ORGANIZATION_ID}', 'Skillmind', 'ACTIVE', '{{}}'::json, 90,
               '2026-07-03T00:00:00+00:00', '2026-07-03T00:00:00+00:00')"""
        )
    )
