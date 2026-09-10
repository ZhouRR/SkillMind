"""取得済みの会話と user を、認証と管理 transaction の両方で検証する。"""

from __future__ import annotations

import hmac
from datetime import datetime

from skillmind.auth.domain import (
    SESSION_CREDENTIAL_VERSION,
    derive_session_csrf,
    hash_session_secret,
)
from skillmind.db.models import AuthSession, User


class UnauthorizedSessionError(RuntimeError):
    """Session が欠落・失効・期限切れ、または現在の user と不整合であることを示す。"""


class CsrfRejectedError(RuntimeError):
    """現在の Session に対する CSRF token が一致しないことを示す。"""


def validate_session_credentials(
    auth_session: AuthSession,
    user: User,
    *,
    session_token: str,
    now: datetime,
) -> None:
    """全 lock 待ち後の時刻で owner、両 hash、版、role と期限を一度に検証する。"""

    try:
        expected_csrf_hash = hash_session_secret(derive_session_csrf(session_token))
    except ValueError as error:
        raise UnauthorizedSessionError("Authentication is required") from error
    if (
        auth_session.user_id != user.id
        or not hmac.compare_digest(auth_session.token_hash, hash_session_secret(session_token))
        or auth_session.revoked_at is not None
        or auth_session.idle_expires_at <= now
        or auth_session.absolute_expires_at <= now
        or user.status != "ACTIVE"
        or auth_session.credential_version != SESSION_CREDENTIAL_VERSION
        or auth_session.system_role_at_login != user.system_role
        or not hmac.compare_digest(auth_session.csrf_token_hash, expected_csrf_hash)
    ):
        raise UnauthorizedSessionError("Authentication is required")


def validate_session_csrf(auth_session: AuthSession, csrf_token: str) -> None:
    """書込の本人確認を会話原値と分離し、どの use case でも同じ拒否にする。"""

    if not csrf_token or not hmac.compare_digest(
        auth_session.csrf_token_hash, hash_session_secret(csrf_token)
    ):
        raise CsrfRejectedError("CSRF token was rejected")
