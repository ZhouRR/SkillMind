"""Application 設定の入力制約を検証する。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from projectmind.core.settings import Settings


def test_context_path_accepts_nested_absolute_path() -> None:
    """Context path が複数 segment の絶対 path を受け入れることを確認する。"""

    settings = Settings(context_path="/suite/mind", _env_file=None)
    assert settings.context_path == "/suite/mind"


def test_context_path_rejects_trailing_slash() -> None:
    """Traefik と Vite の解釈差を防ぐため末尾 slash を拒否することを確認する。"""

    with pytest.raises(ValidationError):
        Settings(context_path="/suite/mind/", _env_file=None)


def test_document_allowlist_covers_preview_formats() -> None:
    """画面 preview 対象(txt/md/html)が既定 allowlist で upload 可能であることを確認する。"""

    allowed = Settings(_env_file=None).document_allowed_content_types

    assert {"text/plain", "text/markdown", "text/html"} <= set(allowed)
    # 実行可能物の許可は別決定が要る。既定に紛れ込ませない。
    assert "application/x-sh" not in allowed


def test_auth_cookie_uses_host_prefix_only_in_production() -> None:
    """Production cookie が __Host- 条件を満たし、HTTP test は安全に検証できる。"""

    production = Settings(environment="production", _env_file=None)
    testing = Settings(environment="test", _env_file=None)

    assert production.auth_session_cookie_name == "__Host-projectmind_session"
    assert production.auth_cookie_secure is True
    assert testing.auth_session_cookie_name == "projectmind_session"
    assert testing.auth_cookie_secure is False
