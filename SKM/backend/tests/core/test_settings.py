"""Application 設定の入力制約を検証する。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from skillmind.core.settings import Settings
from skillmind.storage.factory import create_document_upload_limits


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


@pytest.mark.parametrize("content_type", [
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
])
def test_default_upload_policy_accepts_excel_content_types(content_type: str) -> None:
    """Excel の正規 MIME が既定の upload policy で拒否されないことを確認する。"""

    limits = create_document_upload_limits(Settings(_env_file=None))
    assert limits.validate(
        size=4, content_type=content_type, content=b"test", project_usage_bytes=0,
    ) == content_type


def test_auth_cookie_uses_host_prefix_only_in_production() -> None:
    """Production cookie が __Host- 条件を満たし、HTTP test は安全に検証できる。"""

    production = Settings(environment="production", _env_file=None)
    testing = Settings(environment="test", _env_file=None)

    assert production.auth_session_cookie_name == "__Host-skillmind_session"
    assert production.auth_cookie_secure is True
    assert testing.auth_session_cookie_name == "skillmind_session"
    assert testing.auth_cookie_secure is False


def test_login_protection_settings_are_bounded_and_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源 request と account/組合試行の上限を独立して設定できる。"""

    for name, value in (
        ("AUTH_LOGIN_ATTEMPTS_PER_MINUTE", "4"),
        ("AUTH_LOGIN_ACCOUNT_ATTEMPTS_PER_MINUTE", "12"),
        ("AUTH_LOGIN_SOURCE_REQUESTS_PER_MINUTE", "80"),
        ("AUTH_LOGIN_PROTECTION_TIMEOUT_SECONDS", "1.5"),
    ):
        monkeypatch.setenv(f"SKILLMIND_{name}", value)
    settings = Settings(_env_file=None)
    assert settings.auth_login_attempts_per_minute == 4
    assert settings.auth_login_account_attempts_per_minute == 12
    assert settings.auth_login_source_requests_per_minute == 80
    assert settings.auth_login_protection_timeout_seconds == 1.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("auth_login_account_attempts_per_minute", 0),
        ("auth_login_account_attempts_per_minute", 101),
        ("auth_login_source_requests_per_minute", 1),
        ("auth_login_source_requests_per_minute", 10001),
        ("auth_login_protection_timeout_seconds", 0),
        ("auth_login_protection_timeout_seconds", float("nan")),
        ("auth_login_protection_timeout_seconds", 11),
    ],
)
def test_login_protection_settings_cannot_disable_guards(name: str, value: float) -> None:
    """無効な上限や store の無期限待機を起動時に拒否する。"""

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_preparation_settings_keep_independent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """準備の期限・逐根・総量を、互いの代用品にせず独立設定として保持する。"""

    for name in (
        "RUN_PREPARATION_TIMEOUT_SECONDS",
        "WORKSPACE_MATERIALIZE_TOTAL_MAX_BYTES",
        "WORKSPACE_MATERIALIZE_TOTAL_MAX_FILES",
    ):
        monkeypatch.delenv(f"SKILLMIND_{name}", raising=False)
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

    monkeypatch.setenv("SKILLMIND_RUN_PREPARATION_TIMEOUT_SECONDS", "123")
    monkeypatch.setenv("SKILLMIND_WORKSPACE_MATERIALIZE_TOTAL_MAX_BYTES", "7654321")
    monkeypatch.setenv("SKILLMIND_WORKSPACE_MATERIALIZE_TOTAL_MAX_FILES", "432")
    settings = Settings(_env_file=None)
    assert settings.run_preparation_timeout_seconds == 123
    assert settings.workspace_materialize_total_max_bytes == 7_654_321
    assert settings.workspace_materialize_total_max_files == 432
