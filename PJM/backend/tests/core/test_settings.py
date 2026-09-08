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


def test_preparation_settings_keep_independent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """準備の期限・逐根・総量を、互いの代用品にせず独立設定として保持する。"""

    for name in (
        "RUN_PREPARATION_TIMEOUT_SECONDS",
        "WORKSPACE_MATERIALIZE_TOTAL_MAX_BYTES",
        "WORKSPACE_MATERIALIZE_TOTAL_MAX_FILES",
    ):
        monkeypatch.delenv(f"PROJECTMIND_{name}", raising=False)
    settings = Settings(_env_file=None)
    assert settings.run_preparation_timeout_seconds == 300
    assert settings.workspace_materialize_total_max_bytes == 104_857_600
    assert settings.workspace_materialize_total_max_files == 5_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_preparation_timeout_seconds", 0),
        ("run_preparation_timeout_seconds", 3601),
        ("workspace_materialize_total_max_bytes", 0),
        ("workspace_materialize_total_max_bytes", 536_870_913),
        ("workspace_materialize_total_max_files", 0),
        ("workspace_materialize_total_max_files", 50_001),
    ],
)
def test_preparation_settings_reject_unbounded_values(field: str, value: int) -> None:
    """設定で準備保護を無効化したり、承認した最大量を超えたりできない。"""

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_preparation_settings_read_operator_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数の同名 field が Settings へ反映されることを確認する。"""

    monkeypatch.setenv("PROJECTMIND_RUN_PREPARATION_TIMEOUT_SECONDS", "123")
    monkeypatch.setenv("PROJECTMIND_WORKSPACE_MATERIALIZE_TOTAL_MAX_BYTES", "7654321")
    monkeypatch.setenv("PROJECTMIND_WORKSPACE_MATERIALIZE_TOTAL_MAX_FILES", "432")
    settings = Settings(_env_file=None)
    assert settings.run_preparation_timeout_seconds == 123
    assert settings.workspace_materialize_total_max_bytes == 7_654_321
    assert settings.workspace_materialize_total_max_files == 432
