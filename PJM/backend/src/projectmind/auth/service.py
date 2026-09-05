"""Login CSRF、password login、opaque session の transaction 境界を実装する。"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.domain import (
    UI_LANGUAGES,
    generate_session_credentials,
    hash_password,
    hash_session_secret,
    normalize_email,
    verify_password,
)
from projectmind.db.models import AuthSession, User

_DUMMY_PASSWORD_HASH = hash_password("ProjectMind dummy credential value")


class InvalidCredentialsError(RuntimeError):
    """Account existence を公開しない login failure。"""


class LoginCsrfError(RuntimeError):
    """Login CSRF challenge が欠落、期限切れ、再利用されたことを示す。"""


class LoginRateLimitedError(RuntimeError):
    """短期間の login 試行上限を超えたことを示す。"""


class UnauthorizedSessionError(RuntimeError):
    """Session が存在しない、失効済み、または期限切れであることを示す。"""


class CsrfRejectedError(RuntimeError):
    """認証済み Session の CSRF token が一致しないことを示す。"""


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    """Route/use case が利用できる最小の認証済み user identity。"""

    user_id: UUID
    organization_id: UUID
    email: str
    display_name: str
    system_role: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Login response と Cookie 設定に必要な一回限りの情報。"""

    actor: AuthenticatedActor
    session_token: str
    csrf_token: str
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionResult:
    """有効 Session の actor と新しく rotation した CSRF token。"""

    actor: AuthenticatedActor
    csrf_token: str
    absolute_expires_at: datetime


class AuthService:
    """PostgreSQL session 正本と Redis 短期防御を束ねる認証 use case。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        login_csrf_ttl_seconds: int,
        session_idle_minutes: int,
        session_absolute_hours: int,
        admin_session_absolute_hours: int,
        login_attempts_per_minute: int,
    ) -> None:
        """永続 resource と明示的な timeout/rate policy を保持する。"""

        self._session_factory = session_factory
        self._redis = redis
        self._login_csrf_ttl_seconds = login_csrf_ttl_seconds
        self._session_idle_minutes = session_idle_minutes
        self._session_absolute_hours = session_absolute_hours
        self._admin_session_absolute_hours = admin_session_absolute_hours
        self._login_attempts_per_minute = login_attempts_per_minute

    async def issue_login_csrf(self) -> str:
        """一回だけ消費できる短期 login CSRF challenge を Redis に保存する。"""

        token = secrets.token_urlsafe(32)
        await self._redis.set(
            self._login_csrf_key(token),
            "1",
            ex=self._login_csrf_ttl_seconds,
        )
        return token

    async def login(
        self,
        *,
        email: str,
        password: str,
        login_csrf_header: str,
        login_csrf_cookie: str,
        client_address: str,
    ) -> LoginResult:
        """CSRF と rate limit 後に credential を検証し、新規 Session を作成する。"""

        await self._consume_login_csrf(login_csrf_header, login_csrf_cookie)
        try:
            normalized_email = normalize_email(email)
        except ValueError as error:
            # Email syntax failure も account 不在と同じ公開結果に畳み込み、列挙を防ぐ。
            verify_password(_DUMMY_PASSWORD_HASH, password)
            raise InvalidCredentialsError("Invalid email or password") from error
        rate_key = self._login_rate_key(normalized_email, client_address)
        attempts = await self._redis.incr(rate_key)
        if attempts == 1:
            await self._redis.expire(rate_key, 60)
        if attempts > self._login_attempts_per_minute:
            raise LoginRateLimitedError("Too many login attempts")

        async with self._session_factory() as session, session.begin():
            user = (
                await session.scalars(select(User).where(User.email == normalized_email))
            ).one_or_none()
            password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
            verified, needs_rehash = verify_password(password_hash, password)
            if user is None or user.status != "ACTIVE" or not verified:
                raise InvalidCredentialsError("Invalid email or password")

            now = datetime.now(UTC)
            credentials = generate_session_credentials()
            absolute_hours = (
                self._admin_session_absolute_hours
                if user.system_role == "ADMIN"
                else self._session_absolute_hours
            )
            absolute_expires_at = now + timedelta(hours=absolute_hours)
            session.add(
                AuthSession(
                    id=uuid4(),
                    user_id=user.id,
                    token_hash=credentials.session_token_hash,
                    csrf_token_hash=credentials.csrf_token_hash,
                    created_at=now,
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(minutes=self._session_idle_minutes),
                    absolute_expires_at=absolute_expires_at,
                    revoked_at=None,
                    client_ip_hash=hash_session_secret(client_address),
                    user_agent_hash=None,
                )
            )
            user.last_login_at = now
            if needs_rehash:
                user.password_hash = hash_password(password)
            actor = self._actor(user)

        await self._redis.delete(rate_key)
        return LoginResult(
            actor=actor,
            session_token=credentials.session_token,
            csrf_token=credentials.csrf_token,
            absolute_expires_at=absolute_expires_at,
        )

    async def get_session(self, session_token: str) -> SessionResult:
        """有効 Session を取得し、page reload 用に CSRF token を rotation する。"""

        async with self._session_factory() as session, session.begin():
            auth_session, user = await self._load_active_session(session, session_token)
            credentials = generate_session_credentials()
            auth_session.csrf_token_hash = credentials.csrf_token_hash
            return SessionResult(
                actor=self._actor(user),
                csrf_token=credentials.csrf_token,
                absolute_expires_at=auth_session.absolute_expires_at,
            )

    async def authenticate_session(self, session_token: str) -> AuthenticatedActor:
        """CSRF rotation なしで通常の read request に actor を解決する。"""

        async with self._session_factory() as session, session.begin():
            _, user = await self._load_active_session(session, session_token)
            return self._actor(user)

    async def authenticate_unsafe_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
    ) -> AuthenticatedActor:
        """Unsafe request の Session と CSRF token を同じ transaction で検証する。"""

        async with self._session_factory() as session, session.begin():
            auth_session, user = await self._load_active_session(session, session_token)
            if not hmac.compare_digest(
                auth_session.csrf_token_hash,
                hash_session_secret(csrf_token),
            ):
                raise CsrfRejectedError("CSRF token was rejected")
            return self._actor(user)

    async def get_ui_language(self, *, actor: AuthenticatedActor) -> str | None:
        """User 自身の保存済み UI 言語 preference を返す(NULL は browser 追従)。"""

        async with self._session_factory() as session:
            user = await session.get(User, actor.user_id)
            if user is None:
                # 認証済み actor の行が消えるのは削除競合だけであり、hidden 401 に畳む。
                raise UnauthorizedSessionError("hidden")
            return user.ui_language

    async def set_ui_language(
        self,
        *,
        actor: AuthenticatedActor,
        ui_language: str | None,
    ) -> str | None:
        """User 自身の UI 言語 preference を保存する(None で未設定へ戻す)。

        API 層の Literal 検証に加え、DB check 制約と同じ許可集合を service でも強制し、
        別経路から不正値が永続化されるのを防ぐ。
        """

        if ui_language is not None and ui_language not in UI_LANGUAGES:
            raise ValueError(f"Unsupported UI language: {ui_language}")
        async with self._session_factory() as session, session.begin():
            user = await session.get(User, actor.user_id, with_for_update=True)
            if user is None:
                raise UnauthorizedSessionError("hidden")
            user.ui_language = ui_language
            return ui_language

    async def logout(self, *, session_token: str, csrf_token: str) -> None:
        """CSRF 検証後に Session を明示失効させる。"""

        async with self._session_factory() as session, session.begin():
            auth_session, _ = await self._load_active_session(session, session_token)
            if not hmac.compare_digest(
                auth_session.csrf_token_hash,
                hash_session_secret(csrf_token),
            ):
                raise CsrfRejectedError("CSRF token was rejected")
            auth_session.revoked_at = datetime.now(UTC)

    async def _consume_login_csrf(self, header: str, cookie: str) -> None:
        """Double-submit 値と Redis challenge を同時に検証して再利用を防ぐ。"""

        if not header or not cookie or not hmac.compare_digest(header, cookie):
            raise LoginCsrfError("Login CSRF challenge was rejected")
        consumed = await self._redis.getdel(self._login_csrf_key(header))
        if consumed is None:
            raise LoginCsrfError("Login CSRF challenge was rejected")

    async def _load_active_session(
        self,
        session: AsyncSession,
        session_token: str,
    ) -> tuple[AuthSession, User]:
        """Token hash と user status/期限をまとめて fail closed で検証する。"""

        now = datetime.now(UTC)
        row = (
            await session.execute(
                select(AuthSession, User)
                .join(User, User.id == AuthSession.user_id)
                .where(AuthSession.token_hash == hash_session_secret(session_token))
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise UnauthorizedSessionError("Authentication is required")
        auth_session, user = row
        if (
            auth_session.revoked_at is not None
            or auth_session.idle_expires_at <= now
            or auth_session.absolute_expires_at <= now
            or user.status != "ACTIVE"
        ):
            raise UnauthorizedSessionError("Authentication is required")
        # 5 分単位で更新し、通常 request ごとの DB write amplification を避ける。
        if auth_session.last_seen_at <= now - timedelta(minutes=5):
            auth_session.last_seen_at = now
            auth_session.idle_expires_at = min(
                now + timedelta(minutes=self._session_idle_minutes),
                auth_session.absolute_expires_at,
            )
        return auth_session, user

    @staticmethod
    def _actor(user: User) -> AuthenticatedActor:
        """Database model から password を含まない actor を生成する。"""

        return AuthenticatedActor(
            user_id=user.id,
            organization_id=user.organization_id,
            email=user.email,
            display_name=user.display_name,
            system_role=user.system_role,
        )

    @staticmethod
    def _login_csrf_key(token: str) -> str:
        """Redis key に challenge 平文を含めない固定 namespace を返す。"""

        return f"projectmind:auth:login-csrf:{hash_session_secret(token)}"

    @staticmethod
    def _login_rate_key(email: str, client_address: str) -> str:
        """Email/IP を Redis key や運用画面へ露出しない rate-limit key を返す。"""

        return f"projectmind:auth:login-rate:{hash_session_secret(email + '|' + client_address)}"
