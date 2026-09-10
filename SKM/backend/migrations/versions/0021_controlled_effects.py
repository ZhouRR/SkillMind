"""Integration、ResourceBinding と controlled effect 監査 spine を追加する。

Revision ID: 0021_controlled_effects
Revises: 0020_interactive_run
Create Date: 2026-07-18

docs/01 §15 S2/S5。Secret の正文は保存せず locator metadata のみを保持する。Run level の
ResourceBinding、Proposal、Approval、EffectExecution は監査 snapshot なので更新による上書きを
前提にしない。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_controlled_effects"
down_revision: str | None = "0020_interactive_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Project resource と外部 effect の fail-closed 永続 schema を追加する。"""

    op.create_table(
        "secret_references",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("resolver", sa.String(length=32), nullable=False),
        sa.Column("locator", sa.String(length=512), nullable=False),
        sa.Column("key_version", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resolver IN ('ENVIRONMENT', 'FILE')", name="secret_references_resolver"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name="secret_references_status"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_secret_references_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret_references")),
        sa.UniqueConstraint(
            "project_id", "name", name="uq_secret_references_project_name"
        ),
    )
    for column in ("project_id", "created_by"):
        op.create_index(
            op.f(f"ix_secret_references_{column}"),
            "secret_references",
            [column],
            unique=False,
        )

    op.create_table(
        "integrations",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("secret_reference_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name="integrations_status"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_integrations_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["secret_reference_id"],
            ["secret_references.id"],
            name=op.f("fk_integrations_secret_reference_id_secret_references"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint("project_id", "name", name="uq_integrations_project_name"),
    )
    for column in ("project_id", "secret_reference_id", "created_by"):
        op.create_index(
            op.f(f"ix_integrations_{column}"),
            "integrations",
            [column],
            unique=False,
        )

    op.create_table(
        "resource_bindings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("scope_level", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("requirement_key", sa.String(length=128), nullable=False),
        sa.Column("resource_kind", sa.String(length=64), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("source_binding_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("capability_version", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.String(length=128), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(length=71), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_level IN ('PROJECT_DEFAULT', 'TASK', 'RUN')",
            name="resource_bindings_scope_level",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_resource_bindings_integration_id_integrations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_resource_bindings_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_resource_bindings_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["resource_bindings.id"],
            name=op.f("fk_resource_bindings_source_binding_id_resource_bindings"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_bindings")),
        sa.UniqueConstraint(
            "project_id",
            "scope_level",
            "scope_key",
            "requirement_key",
            name="uq_resource_bindings_scope_requirement",
        ),
    )
    for column in (
        "project_id",
        "integration_id",
        "run_id",
        "source_binding_id",
        "created_by",
    ):
        op.create_index(
            op.f(f"ix_resource_bindings_{column}"),
            "resource_bindings",
            [column],
            unique=False,
        )

    op.create_table(
        "effect_preauthorizations",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("capability_version", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("max_risk_level", sa.String(length=16), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_risk_level = 'LOW'", name="effect_preauthorizations_low_risk_only"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name="effect_preauthorizations_status"
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_effect_preauthorizations_integration_id_integrations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_effect_preauthorizations_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_effect_preauthorizations")),
    )
    for column in ("project_id", "integration_id", "created_by", "expires_at"):
        op.create_index(
            op.f(f"ix_effect_preauthorizations_{column}"),
            "effect_preauthorizations",
            [column],
            unique=False,
        )

    op.create_table(
        "change_proposals",
        sa.Column("proposal_ref", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_segment_id", sa.Uuid(), nullable=False),
        sa.Column("run_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("agent_session_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("target_binding_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("effect_intent_key", sa.String(length=128), nullable=False),
        sa.Column("capability_version", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("target_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("precondition_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("rollback_json", sa.JSON(), nullable=False),
        sa.Column("verification_json", sa.JSON(), nullable=False),
        sa.Column("continuation_mode", sa.String(length=16), nullable=False),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False),
        sa.Column("checkpoint_checksum", sa.String(length=71), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=71), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "risk_level IN ('LOW', 'MEDIUM', 'HIGH')", name="change_proposals_risk"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'APPLYING', "
            "'APPLIED', 'REJECTED', 'STALE', 'FAILED')",
            name="change_proposals_status",
        ),
        sa.ForeignKeyConstraint(
            ["agent_session_id"],
            ["agent_sessions.id"],
            name=op.f("fk_change_proposals_agent_session_id_agent_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"],
            ["integrations.id"],
            name=op.f("fk_change_proposals_integration_id_integrations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_change_proposals_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_attempt_id"],
            ["run_attempts.id"],
            name=op.f("fk_change_proposals_run_attempt_id_run_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_change_proposals_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_segment_id"],
            ["run_segments.id"],
            name=op.f("fk_change_proposals_run_segment_id_run_segments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_version_id"],
            ["skill_versions.id"],
            name=op.f("fk_change_proposals_skill_version_id_skill_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_binding_id"],
            ["resource_bindings.id"],
            name=op.f("fk_change_proposals_target_binding_id_resource_bindings"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_proposals")),
        sa.UniqueConstraint("proposal_ref", name="uq_change_proposals_ref"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_change_proposals_run_idempotency"
        ),
    )
    for column in (
        "proposal_ref",
        "project_id",
        "run_id",
        "run_segment_id",
        "run_attempt_id",
        "agent_session_id",
        "skill_version_id",
        "target_binding_id",
        "integration_id",
        "expires_at",
    ):
        op.create_index(
            op.f(f"ix_change_proposals_{column}"),
            "change_proposals",
            [column],
            unique=False,
        )

    op.add_column("user_interactions", sa.Column("change_proposal_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_user_interactions_change_proposal_id_change_proposals"),
        "user_interactions",
        "change_proposals",
        ["change_proposal_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_user_interactions_change_proposal_id"),
        "user_interactions",
        ["change_proposal_id"],
        unique=True,
    )

    op.create_table(
        "change_approvals",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("preauthorization_id", sa.Uuid(), nullable=True),
        sa.Column("proposal_version", sa.Integer(), nullable=False),
        sa.Column("proposal_checksum", sa.String(length=71), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="change_approvals_decision"
        ),
        sa.CheckConstraint(
            "source IN ('USER', 'PREAUTHORIZATION')", name="change_approvals_source"
        ),
        sa.ForeignKeyConstraint(
            ["preauthorization_id"],
            ["effect_preauthorizations.id"],
            name=op.f(
                "fk_change_approvals_preauthorization_id_effect_preauthorizations"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["change_proposals.id"],
            name=op.f("fk_change_approvals_proposal_id_change_proposals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_change_approvals_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_approvals")),
        sa.UniqueConstraint("proposal_id", name="uq_change_approvals_proposal"),
        sa.UniqueConstraint(
            "run_id", "idempotency_key", name="uq_change_approvals_run_idempotency"
        ),
    )
    for column in ("proposal_id", "run_id", "actor_id", "preauthorization_id"):
        op.create_index(
            op.f(f"ix_change_approvals_{column}"),
            "change_approvals",
            [column],
            unique=False,
        )

    op.create_table(
        "effect_executions",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_version", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("before_ref", sa.String(length=64), nullable=True),
        sa.Column("after_ref", sa.String(length=64), nullable=True),
        sa.Column("verification_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'LEASED', 'APPLYING', 'APPLIED', 'STALE', "
            "'FAILED', 'VERIFICATION_FAILED')",
            name="effect_executions_status",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["change_approvals.id"],
            name=op.f("fk_effect_executions_approval_id_change_approvals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["change_proposals.id"],
            name=op.f("fk_effect_executions_proposal_id_change_proposals"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_effect_executions_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["tool_calls.id"],
            name=op.f("fk_effect_executions_tool_call_id_tool_calls"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_effect_executions")),
        sa.UniqueConstraint("approval_id", name="uq_effect_executions_approval"),
        sa.UniqueConstraint("proposal_id", name="uq_effect_executions_proposal"),
    )
    for column in ("proposal_id", "run_id", "approval_id"):
        op.create_index(
            op.f(f"ix_effect_executions_{column}"),
            "effect_executions",
            [column],
            unique=False,
        )
    op.create_index(
        op.f("ix_effect_executions_tool_call_id"),
        "effect_executions",
        ["tool_call_id"],
        unique=True,
    )
    op.add_column(
        "run_results",
        sa.Column(
            "change_proposal_refs_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.alter_column("run_results", "change_proposal_refs_json", server_default=None)


def downgrade() -> None:
    """Controlled effect と resource binding の追加 schema を依存順に除去する。"""

    op.drop_column("run_results", "change_proposal_refs_json")

    op.drop_index(
        op.f("ix_effect_executions_tool_call_id"), table_name="effect_executions"
    )
    for column in ("approval_id", "run_id", "proposal_id"):
        op.drop_index(
            op.f(f"ix_effect_executions_{column}"), table_name="effect_executions"
        )
    op.drop_table("effect_executions")

    for column in ("preauthorization_id", "actor_id", "run_id", "proposal_id"):
        op.drop_index(
            op.f(f"ix_change_approvals_{column}"), table_name="change_approvals"
        )
    op.drop_table("change_approvals")

    op.drop_index(
        op.f("ix_user_interactions_change_proposal_id"), table_name="user_interactions"
    )
    op.drop_constraint(
        op.f("fk_user_interactions_change_proposal_id_change_proposals"),
        "user_interactions",
        type_="foreignkey",
    )
    op.drop_column("user_interactions", "change_proposal_id")

    for column in (
        "expires_at",
        "integration_id",
        "target_binding_id",
        "skill_version_id",
        "agent_session_id",
        "run_attempt_id",
        "run_segment_id",
        "run_id",
        "project_id",
        "proposal_ref",
    ):
        op.drop_index(
            op.f(f"ix_change_proposals_{column}"), table_name="change_proposals"
        )
    op.drop_table("change_proposals")

    for column in ("expires_at", "created_by", "integration_id", "project_id"):
        op.drop_index(
            op.f(f"ix_effect_preauthorizations_{column}"),
            table_name="effect_preauthorizations",
        )
    op.drop_table("effect_preauthorizations")

    for column in (
        "created_by",
        "source_binding_id",
        "run_id",
        "integration_id",
        "project_id",
    ):
        op.drop_index(
            op.f(f"ix_resource_bindings_{column}"), table_name="resource_bindings"
        )
    op.drop_table("resource_bindings")

    for column in ("created_by", "secret_reference_id", "project_id"):
        op.drop_index(op.f(f"ix_integrations_{column}"), table_name="integrations")
    op.drop_table("integrations")

    for column in ("created_by", "project_id"):
        op.drop_index(
            op.f(f"ix_secret_references_{column}"), table_name="secret_references"
        )
    op.drop_table("secret_references")
