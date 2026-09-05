"""RunSkillSnapshot と M0 JAF Native published seed を追加する。

Revision ID: 0009_run_skill_snapshots
Revises: 0008_skill_versions
Create Date: 2026-07-03
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0009_run_skill_snapshots"
down_revision: str | None = "0008_skill_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_PROJECT_ID = UUID("00000000-0000-4000-8000-000000000200")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000201")
SKILL_ID = UUID("00000000-0000-4000-8000-000000000202")
VERSION_ID = UUID("00000000-0000-4000-8000-000000000203")
MANIFEST_ID = UUID("00000000-0000-4000-8000-000000000204")
INTERPRETATION_ID = UUID("00000000-0000-4000-8000-000000000001")
SOURCE_HASH = "sha256:" + ("0" * 64)
MANIFEST_CHECKSUM = "sha256:92c7726b88f205059de7f62bebf2ff1560d69bdfb28112fe02856569fc3e6ddc"

MANIFEST = json.loads(
    r"""{
      "manifest_version":"projectmind/v1alpha1",
      "identity":{"skill_key":"jaf-quality-analysis","source_hash":"sha256:0000000000000000000000000000000000000000000000000000000000000000","interpretation_id":"00000000-0000-4000-8000-000000000001","interpreter_version":"manual-seed/0.1.0"},
      "compatibility":{"level":"native","confidence":1,"diagnostics":[]},
      "capabilities":[
        {"key":"jaf.ticket.analyze","title":"JAF Ticket 品质分析","triggers":["ticket-analysis"]}
      ],
      "tasks":[{"key":"analyze-ticket","capability":"jaf.ticket.analyze","type":"immediate","input_schema":{"$ref":"jaf/ticket-analyze/v1/input.schema.json"},"output_schema":{"$ref":"jaf/ticket-analyze/v1/output.schema.json"},"workflow":"analyze-ticket-v1","view":"jaf-ticket-quality-report"}],
      "data_sources":[{"key":"issue-source","capability":"issue.read/v1","required":true,"accepted_providers":["csv","redmine"],"selection_policy":"ai_then_user"},{"key":"repository-source","capability":"repository.read/v1","required":false,"accepted_providers":["git","svn"],"selection_policy":"ai_then_user"}],
      "tools":[{"capability":"issue.read/v1","required":true},{"capability":"repository.read/v1","required":false}],
      "workflows":[{"key":"analyze-ticket-v1","steps":[{"key":"load-ticket","kind":"tool"},{"key":"collect-evidence","kind":"agent"},{"key":"analyze","kind":"agent"},{"key":"validate-result","kind":"validator"},{"key":"export-report","kind":"artifact"}]}],
      "permissions":{"default_tool_policy":"auto","registered_script_policy":"auto","external_write_policy":"deny","write_capabilities":[],"network_scope":"project_integrations_only"},
      "ui":{"default_view":"jaf-ticket-quality-report","views":[{"$ref":"examples/jaf-ticket-view.v1alpha1.json"}],"frontend_module":null},
      "tests":[{"key":"ticket-minimal","type":"schema","fixture":"examples/jaf-ticket-input.v1.json"}]
    }"""
)


def upgrade() -> None:
    """Run/version binding table を作成し、検証済み M0 Native version を投入する。"""

    op.create_table(
        "run_skill_snapshots",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("manifest_checksum", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_skill_snapshots")),
        sa.UniqueConstraint("run_id", "sort_order", name="uq_run_skill_snapshots_run_order"),
        sa.UniqueConstraint(
            "run_id", "skill_version_id", name="uq_run_skill_snapshots_run_version"
        ),
    )
    op.create_index(op.f("ix_run_skill_snapshots_run_id"), "run_skill_snapshots", ["run_id"])
    op.create_index(
        op.f("ix_run_skill_snapshots_skill_version_id"),
        "run_skill_snapshots",
        ["skill_version_id"],
    )

    now = datetime(2026, 7, 3, tzinfo=UTC)
    _insert_seed_rows(now)
    # M0 既存 Run も監査上の version/checksum を辿れるよう追加 table だけを backfill する。
    # Run 本体の不変 task snapshot は書き換えない。
    _execute_literal_sql(
        f"""WITH candidates AS (
          SELECT r.id AS run_id, md5(r.id::text || '{VERSION_ID}') AS digest
          FROM runs r
        )
        INSERT INTO run_skill_snapshots
          (id, run_id, skill_version_id, sort_order, config_snapshot_json,
           manifest_checksum, created_at)
        SELECT (substr(digest,1,8)||'-'||substr(digest,9,4)||'-'||substr(digest,13,4)||'-'||
                substr(digest,17,4)||'-'||substr(digest,21,12))::uuid,
               run_id, '{VERSION_ID}', 0, '{{}}'::json,
               '{MANIFEST_CHECKSUM}', '{now.isoformat()}'
        FROM candidates"""
    )


def _insert_seed_rows(now: datetime) -> None:
    """Offline SQL でも再現できる固定 literal で seed aggregate を投入する。"""

    manifest = json.dumps(MANIFEST, ensure_ascii=False, separators=(",", ":")).replace("'", "''")
    timestamp = now.isoformat()
    _execute_literal_sql(
        f"""INSERT INTO skill_sources
        (id, project_id, name, source_type, storage_uri, content_hash, source_version,
         imported_by, source_snapshot_json, created_at)
        VALUES ('{SOURCE_ID}', '{SYSTEM_PROJECT_ID}', 'JAF Quality Analysis', 'manual_seed',
        'contract://examples/jaf-ticket-manifest.v1alpha1.json', '{SOURCE_HASH}', 'm0/v1',
        '{SYSTEM_PROJECT_ID}', '[]'::json, '{timestamp}')"""
    )
    _execute_literal_sql(
        f"""INSERT INTO skill_interpretations
        (id, skill_source_id, origin, interpreter_version, model, compatibility_level,
         status, summary, confidence, assumptions_json, questions_json, diagnostics_json,
         normalized_package_json, manifest_draft_json, checksum, created_at)
        VALUES ('{INTERPRETATION_ID}', '{SOURCE_ID}', 'manual_seed', 'manual-seed/0.1.0',
        NULL, 'native', 'PREVIEW_READY', 'M0 frozen JAF ticket analysis', 1.0,
        '[]'::json, '[]'::json, '[]'::json,
        '{{"package_format":"projectmind.manual-seed/v1"}}'::json,
        '{manifest}'::json, '{MANIFEST_CHECKSUM}', '{timestamp}')"""
    )
    _execute_literal_sql(
        f"""INSERT INTO skills
        (id, key, name, description, status, created_at, updated_at)
        VALUES ('{SKILL_ID}', 'jaf-quality-analysis', 'JAF Quality Analysis',
        'M0 frozen Native ticket analysis', 'PUBLISHED', '{timestamp}', '{timestamp}')"""
    )
    _execute_literal_sql(
        f"""INSERT INTO skill_versions
        (id, skill_id, version, skill_source_id, interpretation_id, status, gate_report_json,
         published_by, published_at, created_at, updated_at)
        VALUES ('{VERSION_ID}', '{SKILL_ID}', '1.0.0', '{SOURCE_ID}', '{INTERPRETATION_ID}',
        'PUBLISHED', '{{"passed":true,"findings":[],"interpretation_diff":{{}},
        "accepted_warnings":[]}}'::json, '{SYSTEM_PROJECT_ID}', '{timestamp}',
        '{timestamp}', '{timestamp}')"""
    )
    _execute_literal_sql(
        f"""INSERT INTO runtime_manifests
        (id, interpretation_id, skill_version_id, manifest_version, manifest_json,
         checksum, created_at)
        VALUES ('{MANIFEST_ID}', '{INTERPRETATION_ID}', '{VERSION_ID}',
        'projectmind/v1alpha1', '{manifest}'::json, '{MANIFEST_CHECKSUM}', '{timestamp}')"""
    )


def _execute_literal_sql(statement: str) -> None:
    """固定 seed JSON 内の colon を bind parameter と解釈させず driver へ渡す。"""

    # sa.text() は JSON の `:1` / `:true` / `:false` も bind parameter と解釈するため、
    # repository 内で固定した literal だけを
    # DBAPI driver の raw SQL 経路で実行する。
    op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    """Run binding table と固定 seed を依存関係の逆順で削除する。"""

    op.drop_table("run_skill_snapshots")
    op.execute(sa.text("DELETE FROM runtime_manifests WHERE id = :id").bindparams(id=MANIFEST_ID))
    op.execute(sa.text("DELETE FROM skill_versions WHERE id = :id").bindparams(id=VERSION_ID))
    op.execute(sa.text("DELETE FROM skills WHERE id = :id").bindparams(id=SKILL_ID))
    op.execute(
        sa.text("DELETE FROM skill_interpretations WHERE id = :id").bindparams(id=INTERPRETATION_ID)
    )
    op.execute(sa.text("DELETE FROM skill_sources WHERE id = :id").bindparams(id=SOURCE_ID))
