"""HTTP API と原生 SQL へ既存接続を移す。資格情報の byte は変更しない。"""

from alembic import op

revision = "0059_native_resource_clients"
down_revision = "0058_document_file_mutations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """明示依頼に従い原 DB account を維持する。旧 Run の凍結 scope は書き換えない。"""
    op.execute("""
        UPDATE integrations SET
          capabilities_json = CASE WHEN capabilities_json::jsonb ? 'database.write/v1'
            THEN '["database.query/v1","database.execute/v1"]'::json
            ELSE '["database.query/v1"]'::json END,
          scope_json = CASE WHEN capabilities_json::jsonb ? 'database.write/v1'
            THEN '{"statements":["SELECT","INSERT","UPDATE","DELETE"]}'::json
            ELSE '{"statements":["SELECT"]}'::json END,
          config_json = (config_json::jsonb || '{"access_mode":"native_sql"}'::jsonb)::json,
          revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE provider = 'postgres' AND NOT (capabilities_json::jsonb ? 'database.query/v1')
    """)
    op.execute("""
        UPDATE integrations SET provider = 'http', kind = 'other',
          capabilities_json = CASE WHEN capabilities_json::jsonb ? 'issue.update/v1'
            THEN '["http.read/v1","http.write/v1"]'::json ELSE '["http.read/v1"]'::json END,
          scope_json = json_build_object('paths',
            CASE WHEN scope_json::jsonb -> 'issue_ids' ? '*'
              THEN '["/issues"]'::jsonb ELSE (SELECT jsonb_agg('/issues/' || value || '.json')
                FROM jsonb_array_elements_text(scope_json::jsonb -> 'issue_ids')) END,
            'methods', CASE WHEN capabilities_json::jsonb ? 'issue.update/v1'
              THEN '["GET","HEAD","PUT"]'::jsonb ELSE '["GET","HEAD"]'::jsonb END),
          config_json = (config_json::jsonb || '{"auth_mode":"header","credential_header":"X-Redmine-API-Key"}'::jsonb)::json,
          revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE provider = 'redmine'
    """)
    # Managed secret の AAD は project_id/reference_id のため、この表示分類変更で再暗号化しない。
    op.execute("UPDATE secret_references SET provider = 'http' WHERE provider = 'redmine'")
    # 旧要求への default を別の意味の能力へ流用しない。Run とその audit はそのまま保持する。
    op.execute("""
        UPDATE resource_bindings SET disabled_at = COALESCE(disabled_at, CURRENT_TIMESTAMP)
        WHERE scope_level <> 'RUN' AND provider IN ('redmine', 'postgres')
          AND capability_version IN ('issue.read/v1','issue.update/v1','database.read/v1','database.write/v1')
    """)


def downgrade() -> None:
    """新しい API scope を旧 field/table scope に推測変換しない。"""
    raise RuntimeError("Native resource configuration cannot be downgraded automatically")
