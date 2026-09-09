"""管理 transaction の原 credential と最新 actor を共通の境界で再検証する。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from projectmind.auth.domain import derive_session_csrf
from projectmind.auth.sessions import (
    UnauthorizedSessionError,
    validate_session_credentials,
    validate_session_csrf,
)
from projectmind.users.domain import UserAccess, UserAdministrationDeniedError
from projectmind.users.repository import LockedUsers


def validate_user_access(access: UserAccess) -> None:
    """DB 待機前に原 credential の形式とサーバー生成 request UUID を確認する。"""

    try:
        derive_session_csrf(access.session_token)
    except ValueError as error:
        raise UnauthorizedSessionError("Authentication is required") from error
    if not isinstance(access.request_id, UUID):
        raise ValueError("Security audit requires a server request UUID")


def authorize_user_access(
    access: UserAccess, locked: LockedUsers, *, now: datetime, admin: bool, write: bool
) -> datetime:
    """全待機後の時刻で原会話・現在 role・CSRF を検証し、古い actor を信用しない。"""

    if (
        locked.actor.id != access.actor.user_id
        or locked.actor.organization_id != access.actor.organization_id
    ):
        raise UnauthorizedSessionError("Authentication is required")
    validate_session_credentials(
        locked.current_session, locked.actor, session_token=access.session_token, now=now
    )
    if write:
        validate_session_csrf(locked.current_session, access.csrf_token)
    if admin and locked.actor.system_role != "ADMIN":
        raise UserAdministrationDeniedError("Administrator access is required")
    return now
