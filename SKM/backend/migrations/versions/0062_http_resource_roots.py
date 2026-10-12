"""HTTP 接続を根 URL 一覧と認証だけへ移し、GET 回読前提を廃止する。"""

from __future__ import annotations

from alembic import op

revision = "0062_http_resource_roots"
down_revision = "0061_skill_source_entry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """現在の接続を一度だけ変換する。原 Run/Effect の凍結内容や資格 byte は変えない。"""
    op.execute("""
        UPDATE integrations SET
          scope_json = json_build_object('base_urls', json_build_array(config_json->>'base_url')),
          config_json = (config_json::jsonb - 'base_url')::json,
          revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE provider = 'http' AND config_json::jsonb ? 'base_url'
    """)
    # 旧 scope/revision の default は新しい意味に流用しない。新 Run は現在の接続を選択する。
    op.execute("""
        UPDATE resource_bindings SET disabled_at = COALESCE(disabled_at, CURRENT_TIMESTAMP)
        WHERE provider = 'http' AND scope_level <> 'RUN'
          AND scope_json::jsonb ? 'paths'
    """)


def downgrade() -> None:
    """複数根を単一 URL と旧方法制約へ推測変換しない。"""
    raise RuntimeError("HTTP root-list configuration cannot be downgraded automatically")
