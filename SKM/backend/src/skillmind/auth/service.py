"""Login CSRF、password login、opaque session の transaction 境界を実装する。"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.auth.domain import (
    SESSION_CREDENTIAL_VERSION,
    UI_LANGUAGES,
    derive_session_csrf,
    generate_session_credentials,
    hash_password,
    hash_session_secret,
    normalize_email,
    verify_password,
)
from skillmind.auth.login_protection import (
    LoginAdmission,
    LoginProtection,
    LoginProtectionPolicy,
    LoginProtectionUnavailableError,
)
from skillmind.auth.sessions import (
    CsrfRejectedError as CsrfRejectedError,
)
from skillmind.auth.sessions import (
    UnauthorizedSessionError as UnauthorizedSessionError,
)
from skillmind.auth.sessions import (
    validate_session_credentials,
    validate_session_csrf,
)
from skillmind.db.models import AuthSession, User

_DUMMY_PASSWORD_HASH = hash_password("Skillmind dummy credential value")


class InvalidCredentialsError(RuntimeError):
    """Account existence を公開しない login failure。"""


class LoginCsrfError(RuntimeError):
    """Login CSRF challenge が欠落、期限切れ、再利用されたことを示す。"""


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
    """有効 Session の actor と、他ページを失効させない会話単位の CSRF token。"""

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
        login_account_attempts_per_minute: int,
        login_source_requests_per_minute: int,
        login_protection_timeout_seconds: float,
    ) -> None:
        """永続 resource と明示的な timeout/rate policy を保持する。"""

        self._session_factory = session_factory
        self._redis = redis
        self._login_csrf_ttl_seconds = login_csrf_ttl_seconds
        self._session_idle_minutes = session_idle_minutes
        self._session_absolute_hours = session_absolute_hours
        self._admin_session_absolute_hours = admin_session_absolute_hours
        self._login_protection = LoginProtection(
            redis,
            LoginProtectionPolicy(
                pair_attempts=login_attempts_per_minute,
                account_attempts=login_account_attempts_per_minute,
                source_requests=login_source_requests_per_minute,
                timeout_seconds=login_protection_timeout_seconds,
            ),
        )

    async def begin_login(self, client_address: str) -> LoginAdmission:
        """HTTP body を読む前の来源 admission を一回だけ発行する。"""

        return await self._login_protection.begin(client_address)

    async def issue_login_csrf(self, admission: LoginAdmission) -> str:
        """一回だけ消費できる短期 login CSRF challenge を Redis に保存する。"""

        self._login_protection.consume(admission)
        token = secrets.token_urlsafe(32)
        try:
            async with asyncio.timeout(self._login_protection.policy.timeout_seconds):
                saved = await self._redis.set(
                    self._login_csrf_key(token),
                    "1",
                    ex=self._login_csrf_ttl_seconds,
                )
        except (RedisError, TimeoutError) as error:
            raise LoginProtectionUnavailableError("Login protection is unavailable") from error
        if saved is not True:
            raise LoginProtectionUnavailableError("Login protection is unavailable")
        return token

    async def admit_password_change(
        self, *, actor: AuthenticatedActor, admission: LoginAdmission
    ) -> None:
        """本人改密にも同じ account/source 配額を適用し、別入口による推測を防ぐ。"""

        source_hash = self._login_protection.consume(admission)
        await self._login_protection.check_account(normalize_email(actor.email), source_hash)

    async def login(
        self,
        *,
        email: str,
        password: str,
        login_csrf_header: str,
        login_csrf_cookie: str,
        client_address: str,
        admission: LoginAdmission,
    ) -> LoginResult:
        """CSRF と rate limit 後に credential を検証し、新規 Session を作成する。"""

        source_hash = self._login_protection.consume(admission)
        try:
            normalized_email = normalize_email(email)
        except ValueError as error:
            await self._consume_login_csrf(login_csrf_header, login_csrf_cookie)
            # Email syntax failure も account 不在と同じ公開結果に畳み込み、列挙を防ぐ。
            verify_password(_DUMMY_PASSWORD_HASH, password)
            raise InvalidCredentialsError("Invalid email or password") from error
        await self._login_protection.check_account(normalized_email, source_hash)
        await self._consume_login_csrf(login_csrf_header, login_csrf_cookie)

        async with self._session_factory() as session, session.begin():
            user = (
                await session.scalars(
                    select(User).where(User.email == normalized_email).with_for_update()
                )
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
                    credential_version=SESSION_CREDENTIAL_VERSION,
                    system_role_at_login=user.system_role,
                    created_at=now,
                    last_seen_at=now,
                    idle_expires_at=min(
                        now + timedelta(minutes=self._session_idle_minutes),
                        absolute_expires_at,
                    ),
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

        # 成功で共有 quota を消すと、別の在途試行や攻撃の履歴まで消してしまう。
        # 正しい password の試行も admission に含め、時間経過だけで減衰させる。
        return LoginResult(
            actor=actor,
            session_token=credentials.session_token,
            csrf_token=credentials.csrf_token,
            absolute_expires_at=absolute_expires_at,
        )

    async def get_session(self, session_token: str) -> SessionResult:
        """有効 Session と安定 CSRF を返し、他ページの書込資格を変更しない。"""

        async with self._session_factory() as session, session.begin():
            auth_session, user = await self._load_active_session(session, session_token)
            return SessionResult(
                actor=self._actor(user),
                csrf_token=derive_session_csrf(session_token),
                absolute_expires_at=auth_session.absolute_expires_at,
            )

    async def authenticate_session(self, session_token: str) -> AuthenticatedActor:
        """通常の read request でも会話版・role・失効を確認し actor を解決する。"""

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
            validate_session_csrf(auth_session, csrf_token)
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
            validate_session_csrf(auth_session, csrf_token)
            auth_session.revoked_at = datetime.now(UTC)

    async def _consume_login_csrf(self, header: str, cookie: str) -> None:
        """Double-submit 値と Redis challenge を同時に検証して再利用を防ぐ。"""

        if not header or not cookie or not hmac.compare_digest(header, cookie):
            raise LoginCsrfError("Login CSRF challenge was rejected")
        try:
            async with asyncio.timeout(self._login_protection.policy.timeout_seconds):
                consumed = await self._redis.getdel(self._login_csrf_key(header))
        except (RedisError, TimeoutError) as error:
            raise LoginProtectionUnavailableError("Login protection is unavailable") from error
        if consumed is None:
            raise LoginCsrfError("Login CSRF challenge was rejected")
        if consumed not in ("1", b"1"):
            # 存在するだけでは発行済みを証明できない。破損値を認証成功へ進めない。
            raise LoginProtectionUnavailableError("Login protection is unavailable")

    async def _load_active_session(
        self,
        session: AsyncSession,
        session_token: str,
    ) -> tuple[AuthSession, User]:
        """User → Session の順で lock し、新しい時間とログイン時の権限を検証する。"""

        try:
            derive_session_csrf(session_token)
        except ValueError as error:
            raise UnauthorizedSessionError("Authentication is required") from error
        # 認証と後続の user 管理が逆順に lock して deadlock しないよう、親を先に取る。
        user = (
            await session.scalars(
                select(User)
                .join(AuthSession, AuthSession.user_id == User.id)
                .where(AuthSession.token_hash == hash_session_secret(session_token))
                .with_for_update(of=User)
            )
        ).one_or_none()
        if user is None:
            raise UnauthorizedSessionError("Authentication is required")
        auth_session = (
            await session.scalars(
                select(AuthSession)
                .where(
                    AuthSession.token_hash == hash_session_secret(session_token),
                    AuthSession.user_id == user.id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if auth_session is None:
            raise UnauthorizedSessionError("Authentication is required")
        # 両方の lock 待ちを終えてから判定する。待機前の now で期限を延ばさない。
        now = datetime.now(UTC)
        validate_session_credentials(auth_session, user, session_token=session_token, now=now)
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

        return f"skillmind:auth:login-csrf:{hash_session_secret(token)}"
