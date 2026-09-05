"""認証 credential の domain 不変条件を検証する。"""

from __future__ import annotations

import pytest

from projectmind.auth import (
    PasswordPolicyError,
    generate_session_credentials,
    hash_password,
    hash_session_secret,
    normalize_email,
    verify_password,
)


def test_password_hash_uses_argon2id_and_verifies_unicode_password() -> None:
    """Unicode を含む十分長い password が Argon2id で往復検証できることを確認する。"""

    password = "長い安全な pass phrase 2026"
    password_hash = hash_password(password)

    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, password) == (True, False)
    assert verify_password(password_hash, "incorrect password value") == (False, False)


def test_password_policy_rejects_short_and_oversized_values() -> None:
    """短すぎる password と UTF-8 byte 上限超過を保存前に拒否する。"""

    with pytest.raises(PasswordPolicyError):
        hash_password("short")
    with pytest.raises(PasswordPolicyError):
        hash_password("密" * 400)


def test_session_credentials_store_only_independent_hashes() -> None:
    """Session と CSRF secret が独立し、永続値から平文を復元できない形式であることを確認する。"""

    credentials = generate_session_credentials()

    assert credentials.session_token != credentials.csrf_token
    assert credentials.session_token_hash == hash_session_secret(credentials.session_token)
    assert credentials.csrf_token_hash == hash_session_secret(credentials.csrf_token)
    assert credentials.session_token not in credentials.session_token_hash


def test_email_normalization_is_stable_for_login_lookup() -> None:
    """Email の大小文字と外側空白が一意制約を迂回しないことを確認する。"""

    assert normalize_email(" Admin@Example.COM ") == "admin@example.com"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")
