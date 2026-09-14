"""組織全リソース用 API key を発行・検証・撤銷し、元資格の再検証を共有する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.auth.api_key_domain import CreatedApiKey, StoredApiKey
from skillmind.auth.api_key_repository import ApiKeyRepository
from skillmind.auth.domain import derive_api_key_proof, generate_api_key_credentials
from skillmind.auth.service import AuthenticatedActor, AuthService
from skillmind.auth.sessions import UnauthorizedSessionError, validate_session_credentials
from skillmind.users.access import authorize_user_access, validate_user_access
from skillmind.users.domain import UserAccess
from skillmind.users.repository import UserRepository


class ApiKeyService:
    """キー別の scope は持たず、活動 ADMIN の組織全体へ通常 API を開放する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """資格台帳と同じ DB transaction を所有する。"""
        self.session_factory = session_factory

    async def authenticate(self, token: str) -> tuple[AuthenticatedActor, UUID]:
        """原 key を検証し、browser idle 期限を適用せず利用時刻だけを更新する。"""
        try:
            derive_api_key_proof(token)
        except ValueError as error:
            raise UnauthorizedSessionError("Authentication is required") from error
        async with self.session_factory() as session, session.begin():
            key, credential, user = await ApiKeyRepository(session).authenticate(token)
            now = datetime.now(UTC)
            validate_session_credentials(credential, user, session_token=token, now=now)
            if (
                credential.last_seen_at == credential.created_at
                or now - credential.last_seen_at >= timedelta(minutes=5)
            ):
                credential.last_seen_at = now
            return AuthService._actor(user), key.id

    async def list(self, access: UserAccess) -> tuple[StoredApiKey, ...]:
        """現在の管理者資格を lock 後に再確認して組織のキーを読む。"""
        validate_user_access(access)
        async with self.session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
            )
            authorize_user_access(access, locked, now=datetime.now(UTC), admin=True, write=False)
            return await ApiKeyRepository(session).list(locked.actor.organization_id)

    async def create(self, access: UserAccess, name: str) -> CreatedApiKey:
        """秘密は commit 成功後の一回だけ返し、未知 commit を自動で再発行しない。"""
        name = name.strip()
        if not name or len(name) > 200:
            raise ValueError("API key name must contain 1 to 200 characters")
        validate_user_access(access)
        credentials = generate_api_key_credentials()
        async with self.session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
            )
            now = authorize_user_access(
                access, locked, now=datetime.now(UTC), admin=True, write=True
            )
            record = await ApiKeyRepository(session).create(
                user=locked.actor, name=name, credentials=credentials, now=now
            )
            await session.flush()
            authorize_user_access(access, locked, now=datetime.now(UTC), admin=True, write=True)
        return CreatedApiKey(record, credentials.session_token)

    async def revoke(self, access: UserAccess, key_id: UUID) -> StoredApiKey:
        """対象を持久失効させ、再送でも再有効化しない。自分自身の key 撤銷も許可する。"""
        validate_user_access(access)
        async with self.session_factory() as session, session.begin():
            locked = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
            )
            repository = ApiKeyRepository(session)
            key, credential = await repository.lock_target(locked.actor.organization_id, key_id)
            now = authorize_user_access(
                access, locked, now=datetime.now(UTC), admin=True, write=True
            )
            if credential.revoked_at is None:
                credential.revoked_at = now
            await session.flush()
            # logout と同様、自分の資格を意図的に失効させた後は再認証で rollback しない。
            if credential.id != locked.current_session.id:
                authorize_user_access(access, locked, now=datetime.now(UTC), admin=True, write=True)
            result = repository.stored(key, credential)
        return result
