"""API key の metadata と共通資格台帳を、組織・元 User に束縛して取得する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.auth.api_key_domain import ApiKeyNotFoundError, StoredApiKey
from skillmind.auth.domain import (
    API_KEY_CREDENTIAL_VERSION,
    SessionCredentials,
    hash_session_secret,
)
from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.db.models import ApiKey, AuthSession, User


class ApiKeyRepository:
    """Service が持つ transaction 内だけで lock と明示投影を行う。"""

    def __init__(self, session: AsyncSession) -> None:
        """API/Worker の既存 session factory を再利用する。"""
        self.session = session

    async def authenticate(self, token: str) -> tuple[ApiKey, AuthSession, User]:
        """User → 資格の順で固定し、失効・role 変更との逆順待機を避ける。"""
        token_hash = hash_session_secret(token)
        user = await self.session.scalar(
            select(User)
            .join(AuthSession, AuthSession.user_id == User.id)
            .join(ApiKey, ApiKey.id == AuthSession.id)
            .where(
                AuthSession.token_hash == token_hash, ApiKey.organization_id == User.organization_id
            )
            .with_for_update(of=User)
            .execution_options(populate_existing=True)
        )
        if user is None:
            raise UnauthorizedSessionError("Authentication is required")
        result = (
            await self.session.execute(
                select(ApiKey, AuthSession)
                .join(AuthSession, AuthSession.id == ApiKey.id)
                .where(
                    AuthSession.user_id == user.id,
                    AuthSession.token_hash == token_hash,
                    ApiKey.organization_id == user.organization_id,
                )
                .with_for_update(of=AuthSession)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if result is None:
            raise UnauthorizedSessionError("Authentication is required")
        return result[0], result[1], user

    async def list(self, organization_id: UUID) -> tuple[StoredApiKey, ...]:
        """全キーの管理 metadata を返し、秘密・内部期限は投影しない。"""
        rows = (
            await self.session.execute(
                select(ApiKey, AuthSession)
                .join(AuthSession, AuthSession.id == ApiKey.id)
                .where(ApiKey.organization_id == organization_id)
                .order_by(AuthSession.created_at.desc(), ApiKey.id.desc())
            )
        ).all()
        return tuple(self.stored(key, credential) for key, credential in rows)

    async def lock_target(self, organization_id: UUID, key_id: UUID) -> tuple[ApiKey, AuthSession]:
        """管理者の組織 lock 下で対象資格を取得する。別組織の存在は返さない。"""
        row = (
            await self.session.execute(
                select(ApiKey, AuthSession)
                .join(AuthSession, AuthSession.id == ApiKey.id)
                .where(ApiKey.organization_id == organization_id, ApiKey.id == key_id)
                .with_for_update(of=AuthSession)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise ApiKeyNotFoundError("API key is not available")
        return row[0], row[1]

    async def create(
        self, *, user: User, name: str, credentials: SessionCredentials, now: datetime
    ) -> StoredApiKey:
        """親資格を先に flush し、metadata と同じ transaction で発行する。"""
        credential = AuthSession(
            id=uuid4(),
            user_id=user.id,
            token_hash=credentials.session_token_hash,
            csrf_token_hash=credentials.csrf_token_hash,
            credential_version=API_KEY_CREDENTIAL_VERSION,
            system_role_at_login="ADMIN",
            created_at=now,
            last_seen_at=now,
            idle_expires_at=datetime(9999, 1, 1, tzinfo=UTC),
            absolute_expires_at=datetime(9999, 1, 1, tzinfo=UTC),
            revoked_at=None,
        )
        self.session.add(credential)
        await self.session.flush()
        key = ApiKey(
            id=credential.id,
            organization_id=user.organization_id,
            name=name,
            key_prefix=credentials.session_token[:13],
        )
        self.session.add(key)
        return self.stored(key, credential)

    @staticmethod
    def stored(key: ApiKey, credential: AuthSession) -> StoredApiKey:
        """ORM 本体や credential hash を response に渡さない。"""
        return StoredApiKey(
            key.id,
            key.name,
            key.key_prefix,
            credential.user_id,
            credential.created_at,
            credential.last_seen_at if credential.last_seen_at > credential.created_at else None,
            credential.revoked_at,
        )
