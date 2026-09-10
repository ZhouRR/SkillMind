"""Password と opaque session credential の不変条件を実装する。"""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skillmind.core.hashing import sha256_hex

MIN_PASSWORD_CHARACTERS = 15
MAX_PASSWORD_BYTES = 1024
SESSION_SECRET_BYTES = 32
SESSION_CREDENTIAL_VERSION = 2
_SESSION_TOKEN_PREFIX = "sm2."
_SESSION_TOKEN_PATTERN = re.compile(r"sm2\.[A-Za-z0-9_-]{43}")
_SESSION_CSRF_INFO = b"skillmind.auth.session-csrf/v2"

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
    """一回発行する session と、同じ会話で再取得できる CSRF および永続 hash の組。"""

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
    """256 bit の random session と用途分離した会話単位の CSRF を生成する。"""

    session_token = _SESSION_TOKEN_PREFIX + secrets.token_urlsafe(SESSION_SECRET_BYTES)
    csrf_token = derive_session_csrf(session_token)
    return SessionCredentials(
        session_token=session_token,
        session_token_hash=hash_session_secret(session_token),
        csrf_token=csrf_token,
        csrf_token_hash=hash_session_secret(csrf_token),
    )


def derive_session_csrf(session_token: str) -> str:
    """v2 会話原値から HKDF-SHA256 で安定 CSRF を派生する (docs/09 §4)。

    入力は高 entropy の cookie 原値であり、DB が保持する hash ではない。版と info は
    protocol の一部として固定し、旧 random CSRF へ fallback しない。返り値だけから
    session を取得する経路は設けず、unsafe 認証でも DB の会話と両方の hash を検証する。
    """

    if _SESSION_TOKEN_PATTERN.fullmatch(session_token) is None:
        raise ValueError("Unsupported session credential")
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=SESSION_SECRET_BYTES,
        salt=None,
        info=_SESSION_CSRF_INFO,
    ).derive(session_token.encode("ascii"))
    return "csrf2." + base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
