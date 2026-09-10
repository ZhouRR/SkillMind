"""認証 credential の domain 不変条件を検証する。"""

from __future__ import annotations

import pytest

from skillmind.auth import (
    PasswordPolicyError,
    generate_session_credentials,
    hash_password,
    hash_session_secret,
    normalize_email,
    verify_password,
)
from skillmind.auth.domain import derive_session_csrf


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


@pytest.mark.parametrize("password", ["x" * 8, "x" * 14, "密" * 8, "🙂" * 8, " secret "])
def test_password_policy_accepts_eight_characters_without_normalizing(password: str) -> None:
    """8 文字以上を Unicode code point で数え、空白を除去せず hash へ渡す。"""

    password_hash = hash_password(password)
    assert verify_password(password_hash, password) == (True, False)
    if password != password.strip():
        assert verify_password(password_hash, password.strip()) == (False, False)


@pytest.mark.parametrize("password", ["x" * 7, "密" * 7, "🙂" * 7])
def test_password_policy_rejects_seven_characters_before_hashing(password: str) -> None:
    """UTF-8 byte 数が 8 以上でも、7 文字の password は拒否する。"""

    with pytest.raises(PasswordPolicyError, match="at least 8 characters"):
        hash_password(password)


def test_session_credentials_store_only_separate_hashes() -> None:
    """Session と用途分離した CSRF の原値を、いずれも DB hash に含めない。"""

    credentials = generate_session_credentials()

    assert credentials.session_token != credentials.csrf_token
    assert credentials.session_token_hash == hash_session_secret(credentials.session_token)
    assert credentials.csrf_token_hash == hash_session_secret(credentials.csrf_token)
    assert credentials.session_token not in credentials.session_token_hash
    assert credentials.session_token.startswith("sm2.")
    assert credentials.csrf_token.startswith("csrf2.")
    assert len(credentials.session_token) == 47
    assert len(credentials.csrf_token) == 49


def test_session_csrf_is_stable_and_bound_to_each_random_session() -> None:
    """会話を再取得しても値は変わらず、別の会話へは転用できない。"""

    first, second = generate_session_credentials(), generate_session_credentials()

    assert derive_session_csrf(first.session_token) == first.csrf_token
    assert derive_session_csrf(first.session_token) == derive_session_csrf(first.session_token)
    assert first.csrf_token != second.csrf_token
    assert first.session_token not in first.csrf_token
    with pytest.raises(ValueError):
        derive_session_csrf(first.session_token_hash)


def test_v2_csrf_profile_matches_an_independent_openssl_vector() -> None:
    """公開の合成入力を OpenSSL HKDF と照合した値で info/長さ/encoding を固定する。"""

    assert derive_session_csrf("sm2." + "A" * 43) == (
        "csrf2.FBHCp4SAJ5jWaUN7fNcwWDRO5gQ-MCOMJjndB4ezvNY"
    )


@pytest.mark.parametrize(
    "value", ["", "a" * 43, "sm1." + "a" * 43, "sm2." + "a" * 42, "sm2." + "密" * 43]
)
def test_legacy_and_malformed_session_credentials_are_not_derived(value: str) -> None:
    """旧版・不正長・非 ASCII の値を新 protocol として解釈しない。"""

    with pytest.raises(ValueError, match="Unsupported"):
        derive_session_csrf(value)


def test_email_normalization_is_stable_for_login_lookup() -> None:
    """Email の大小文字と外側空白が一意制約を迂回しないことを確認する。"""

    assert normalize_email(" Admin@Example.COM ") == "admin@example.com"
    with pytest.raises(ValueError):
        normalize_email("not-an-email")
