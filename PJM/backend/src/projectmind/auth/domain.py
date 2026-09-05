"""Password と opaque session credential の不変条件を実装する。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from projectmind.core.hashing import sha256_hex

MIN_PASSWORD_CHARACTERS = 15
MAX_PASSWORD_BYTES = 1024
SESSION_SECRET_BYTES = 32

# UI 言語 preference の許可集合。docs/01 §17 の言語決定と DB check 制約に一致させる。
UI_LANGUAGES = frozenset({"zh", "ja", "en"})

# OWASP の Argon2id 最小基線を明示し、library default の変更で hash 強度が漂流するのを防ぐ。
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


class PasswordPolicyError(ValueError):
    """Password が公開 policy を満たさない場合の domain error。"""


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    """一度だけ client へ返す session/CSRF secret と永続 hash の組。"""

    session_token: str
    session_token_hash: str
    csrf_token: str
    csrf_token_hash: str


def normalize_email(email: str) -> str:
    """組織内一意制約と login lookup に使う email 表現を正規化する。"""

    normalized = email.strip().casefold()
    if not normalized or "@" not in normalized:
        raise ValueError("A valid email address is required")
    return normalized


def validate_password(password: str) -> None:
    """MVP password の長さ境界だけを検証し、脆い composition rule を課さない。"""

    if len(password) < MIN_PASSWORD_CHARACTERS:
        raise PasswordPolicyError(
            f"Password must contain at least {MIN_PASSWORD_CHARACTERS} characters"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"Password must not exceed {MAX_PASSWORD_BYTES} UTF-8 bytes")


def hash_password(password: str) -> str:
    """Policy 検証後の password を Argon2id PHC string へ変換する。"""

    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, bool]:
    """Password を定時間 hash verifier で検証し、rehash 要否も返す。"""

    try:
        verified = _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False, False
    return verified, verified and _PASSWORD_HASHER.check_needs_rehash(password_hash)


def hash_session_secret(secret: str) -> str:
    """Session/CSRF secret を平文保存しない監査用形式へ変換する。"""

    return f"sha256:{sha256_hex(secret)}"


def generate_session_credentials() -> SessionCredentials:
    """Session と CSRF に独立した 256 bit 以上の random secret を生成する。"""

    session_token = secrets.token_urlsafe(SESSION_SECRET_BYTES)
    csrf_token = secrets.token_urlsafe(SESSION_SECRET_BYTES)
    return SessionCredentials(
        session_token=session_token,
        session_token_hash=hash_session_secret(session_token),
        csrf_token=csrf_token,
        csrf_token_hash=hash_session_secret(csrf_token),
    )
