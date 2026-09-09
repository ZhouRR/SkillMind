"""User 変更、会話の持久失効と監査を一つの認可済み transaction に閉じる。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.domain import (
    derive_session_csrf,
    hash_password,
    validate_password,
    verify_password,
)
from projectmind.auth.sessions import (
    UnauthorizedSessionError,
    validate_session_credentials,
    validate_session_csrf,
)
from projectmind.db.models import User
from projectmind.users.domain import (
    CreateUserCommand,
    CurrentPasswordRejectedError,
    LastActiveAdminError,
    StoredUser,
    StoredUserSecurityEvent,
    UpdateUserCommand,
    UserAccess,
    UserAdministrationDeniedError,
    UserEmailConflictError,
    UserMutationResult,
    UserNotFoundError,
    UserRole,
    UserSecurityAction,
    UserStatus,
    UserVersionConflictError,
    validate_display_name,
    validate_version,
)
from projectmind.users.repository import LockedUsers, UserRepository


class UserService:
    """入口認証の後にも原 credential を検証し、最後の活動 ADMIN を保護する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Public actor を使い回すだけではなく、操作ごとの DB 境界を作る。"""

        self._session_factory = session_factory

    @asynccontextmanager
    async def _transaction(
        self,
        access: UserAccess,
        *,
        target_id: UUID | None = None,
        admin: bool,
        write: bool,
    ) -> AsyncIterator[tuple[UserRepository, LockedUsers]]:
        """秘密の形式を先に確認し、全 lock 後の credential 判断を共通化する。"""

        try:
            derive_session_csrf(access.session_token)
        except ValueError as error:
            raise UnauthorizedSessionError("Authentication is required") from error
        if not isinstance(access.request_id, UUID):
            raise ValueError("Security audit requires a server request UUID")
        async with self._session_factory() as session, session.begin():
            repository = UserRepository(session)
            locked = await repository.lock_users(
                access=access, target_id=target_id, include_target_sessions=write
            )
            self._authorize(access, locked, admin=admin, write=write)
            yield repository, locked
            # event と失効の constraint/FK エラーもこの transaction から外へ伝播させる。
            await session.flush()

    @staticmethod
    def _authorize(
        access: UserAccess, locked: LockedUsers, *, admin: bool, write: bool
    ) -> datetime:
        """待機/計算後の現在時刻と現在 role を使用し、古い actor の権限を使わない。"""

        now = datetime.now(UTC)
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

    async def get_account(self, *, access: UserAccess) -> StoredUser:
        """別 user ID の指定を受けず、現在の本人 account だけを返す。"""

        async with self._transaction(access, admin=False, write=False) as (repository, locked):
            return repository.to_stored(locked.actor)

    async def list_users(
        self, *, access: UserAccess, query: str = "", limit: int = 25, offset: int = 0
    ) -> tuple[tuple[StoredUser, ...], int]:
        """組織全体の検索/page を ADMIN の有効会話にだけ公開する。"""

        self._validate_page(limit, offset)
        query = query.strip()
        if len(query) > 200:
            raise ValueError("User query must not exceed 200 characters")
        async with self._transaction(access, admin=True, write=False) as (repository, locked):
            result = await repository.list_users(
                organization_id=locked.actor.organization_id,
                query=query,
                limit=limit,
                offset=offset,
            )
            self._authorize(access, locked, admin=True, write=False)
            return result

    async def list_security_events(
        self, *, access: UserAccess, user_id: UUID, limit: int = 25, offset: int = 0
    ) -> tuple[tuple[StoredUserSecurityEvent, ...], int]:
        """本人または組織 ADMIN に限定し、不存在と他組織を同じ拒否にする。"""

        self._validate_page(limit, offset)
        admin = user_id != access.actor.user_id
        async with self._transaction(access, target_id=user_id, admin=admin, write=False) as (
            repository,
            locked,
        ):
            target = self._target(locked)
            result = await repository.list_events(
                organization_id=target.organization_id,
                user_id=target.id,
                limit=limit,
                offset=offset,
            )
            self._authorize(access, locked, admin=admin, write=False)
            return result

    async def create_user(
        self, *, access: UserAccess, command: CreateUserCommand
    ) -> UserMutationResult:
        """初期 password を一度だけ hash 化し、親 User を先に flush して監査する。"""

        command = command.validated()
        try:
            async with self._transaction(access, admin=True, write=True) as (repository, locked):
                if await repository.email_exists(
                    organization_id=locked.actor.organization_id, email=command.email
                ):
                    raise UserEmailConflictError("User email already exists")
                encoded = await asyncio.to_thread(hash_password, command.password)
                now = self._authorize(access, locked, admin=True, write=True)
                user = User(
                    id=uuid4(),
                    organization_id=locked.actor.organization_id,
                    email=command.email,
                    display_name=command.display_name,
                    password_hash=encoded,
                    system_role=command.system_role.value,
                    status=UserStatus.ACTIVE.value,
                    row_version=1,
                    created_at=now,
                    updated_at=now,
                )
                await repository.insert_user(user)
                repository.append_event(
                    user=user,
                    actor_id=locked.actor.id,
                    request_id=access.request_id,
                    action=UserSecurityAction.CREATED,
                    previous_role=None,
                    previous_status=None,
                    revoked_sessions=0,
                    now=now,
                )
                return UserMutationResult(repository.to_stored(user), 0, False)
        except IntegrityError as error:
            original = error.orig
            candidates = (
                original,
                getattr(original, "__cause__", None),
                getattr(original, "diag", None),
            )
            if any(
                getattr(item, "constraint_name", None) == "uq_users_organization_email"
                for item in candidates
            ):
                raise UserEmailConflictError("User email already exists") from error
            raise

    async def update_user(
        self, *, access: UserAccess, user_id: UUID, command: UpdateUserCommand
    ) -> UserMutationResult:
        """role/status の変更は旧会話を持久失効させ、名前だけの変更とは区別する。"""

        validate_version(command.expected_row_version)
        name = validate_display_name(command.display_name)
        role, status = UserRole(command.system_role), UserStatus(command.status)
        async with self._transaction(access, target_id=user_id, admin=True, write=True) as (
            repository,
            locked,
        ):
            target = self._target(locked, command.expected_row_version)
            if (target.display_name, target.system_role, target.status) == (
                name,
                role.value,
                status.value,
            ):
                return UserMutationResult(repository.to_stored(target), 0, False)
            losing_admin = (
                target.system_role == "ADMIN"
                and target.status == "ACTIVE"
                and (role is not UserRole.ADMIN or status is not UserStatus.ACTIVE)
            )
            if losing_admin and not await repository.has_other_active_admin(target):
                raise LastActiveAdminError("The last active administrator must be retained")
            now = self._authorize(access, locked, admin=True, write=True)
            previous_role, previous_status = target.system_role, target.status
            target.display_name, target.system_role, target.status = name, role.value, status.value
            revoke = (previous_role, previous_status) != (role.value, status.value)
            return self._finish(
                repository,
                locked,
                access,
                target,
                UserSecurityAction.UPDATED,
                previous_role,
                previous_status,
                now=now,
                revoke=revoke,
            )

    async def revoke_sessions(
        self, *, access: UserAccess, user_id: UUID, expected_row_version: int
    ) -> UserMutationResult:
        """既に期限切れの row も失効させ、再有効化で旧 cookie が復活する余地を残さない。"""

        validate_version(expected_row_version)
        admin = user_id != access.actor.user_id
        async with self._transaction(access, target_id=user_id, admin=admin, write=True) as (
            repository,
            locked,
        ):
            target = self._target(locked, expected_row_version)
            now = self._authorize(access, locked, admin=admin, write=True)
            return self._finish(
                repository,
                locked,
                access,
                target,
                UserSecurityAction.SESSIONS_REVOKED,
                target.system_role,
                target.status,
                now=now,
                revoke=True,
            )

    async def change_password(
        self,
        *,
        access: UserAccess,
        current_password: str,
        new_password: str,
        expected_row_version: int,
    ) -> UserMutationResult:
        """入口配額の確認後、本人の現 password と有効会話の両方で改密を認可する。"""

        validate_version(expected_row_version)
        validate_password(new_password)
        if not current_password or len(current_password.encode("utf-8")) > 1024:
            raise CurrentPasswordRejectedError("Current password was rejected")
        async with self._transaction(
            access, target_id=access.actor.user_id, admin=False, write=True
        ) as (repository, locked):
            target = self._target(locked, expected_row_version)
            verified, _ = await asyncio.to_thread(
                verify_password, target.password_hash, current_password
            )
            if not verified:
                raise CurrentPasswordRejectedError("Current password was rejected")
            encoded = await asyncio.to_thread(hash_password, new_password)
            now = self._authorize(access, locked, admin=False, write=True)
            target.password_hash = encoded
            return self._finish(
                repository,
                locked,
                access,
                target,
                UserSecurityAction.PASSWORD_CHANGED,
                target.system_role,
                target.status,
                now=now,
                revoke=True,
            )

    @staticmethod
    def _target(locked: LockedUsers, expected_row_version: int | None = None) -> User:
        """越組織の存在を隠し、lock 後の楽観版だけで書込を決める。"""

        if locked.target is None:
            raise UserNotFoundError("User not found")
        if expected_row_version is not None and locked.target.row_version != expected_row_version:
            raise UserVersionConflictError("User changed; refresh before trying again")
        return locked.target

    @staticmethod
    def _finish(
        repository: UserRepository,
        locked: LockedUsers,
        access: UserAccess,
        target: User,
        action: UserSecurityAction,
        previous_role: str,
        previous_status: str,
        *,
        now: datetime,
        revoke: bool,
    ) -> UserMutationResult:
        """版・失効・監査を同期し、公開応答へ credential や ORM を渡さない。"""

        revoked = 0
        if revoke:
            for session in locked.sessions:
                if session.user_id == target.id and session.revoked_at is None:
                    session.revoked_at = now
                    revoked += 1
        target.row_version += 1
        target.updated_at = now
        repository.append_event(
            user=target,
            actor_id=locked.actor.id,
            request_id=access.request_id,
            action=action,
            previous_role=previous_role,
            previous_status=previous_status,
            revoked_sessions=revoked,
            now=now,
        )
        return UserMutationResult(
            repository.to_stored(target), revoked, locked.current_session.revoked_at is not None
        )

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        """内部 caller でも無限 page や bool を offset として渡させない。"""

        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError("Invalid user page")
