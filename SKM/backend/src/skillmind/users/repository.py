"""User 管理の固定 lock 順序と、明示投影・追加式監査を保持する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from skillmind.auth.domain import hash_session_secret
from skillmind.auth.sessions import UnauthorizedSessionError
from skillmind.db.models import AuthSession, Organization, User, UserSecurityEvent
from skillmind.users.domain import (
    StoredUser,
    StoredUserSecurityEvent,
    UserAccess,
    UserRole,
    UserSecurityAction,
    UserStatus,
)


@dataclass(frozen=True, slots=True)
class LockedUsers:
    """同じ transaction で取得した model を service に渡し、公開 DTO と区別する。"""

    actor: User = field(repr=False)
    target: User | None = field(repr=False)
    current_session: AuthSession = field(repr=False)
    sessions: tuple[AuthSession, ...] = field(repr=False)


def authorization_failure_snapshot(locked: LockedUsers) -> LockedUsers:
    """失敗分類専用の資格値を、expire/lazy load しない新しい transient model へ写す。

    呼出元は認可済みの row lock 保持中、flush 前に呼ぶ。失敗後の新しい now で既存の
    authorize_user_access を再利用するためだけの値であり、成功や新しい write の認可に
    使わない。Session へ add/merge せず、target/他会話や ORM の内部状態を複写しない。
    """

    actor, current = locked.actor, locked.current_session
    return LockedUsers(
        actor=User(
            id=actor.id,
            organization_id=actor.organization_id,
            system_role=actor.system_role,
            status=actor.status,
        ),
        target=None,
        current_session=AuthSession(
            user_id=current.user_id,
            token_hash=current.token_hash,
            csrf_token_hash=current.csrf_token_hash,
            credential_version=current.credential_version,
            system_role_at_login=current.system_role_at_login,
            revoked_at=current.revoked_at,
            idle_expires_at=current.idle_expires_at,
            absolute_expires_at=current.absolute_expires_at,
        ),
        sessions=(),
    )


async def lock_organization(session: AsyncSession, organization_id: UUID) -> None:
    """User 管理・Project 削除・preference の逆順 FK lock を組織 gate で串行化する。"""

    organization = await session.scalar(
        select(Organization).where(Organization.id == organization_id).with_for_update()
    )
    if organization is None:
        raise UnauthorizedSessionError("Authentication is required")


class UserRepository:
    """Organization → ID 順 User → ID 順 Session で管理操作を串行化する。"""

    def __init__(self, session: AsyncSession) -> None:
        """Use case が所有する一つの transaction session を受け取る。"""

        self._session = session

    async def lock_session_reference(
        self, *, organization_id: UUID, user_id: UUID, session_id: UUID
    ) -> LockedUsers:
        """受理済み非同期要求の参照を Org → User SHARE → Session UPDATE で固定する。

        ID は DB の原要求から取得し、Queue や新しい HTTP actor から補わない。
        Cookie/CSRF を保存せず、期限延長・別会話への差替えも行わない。
        """

        await lock_organization(self._session, organization_id)
        actor = await self._session.scalar(
            select(User)
            .where(User.id == user_id, User.organization_id == organization_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if actor is None:
            raise UnauthorizedSessionError("Authentication is required")
        current = await self._session.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current is None:
            raise UnauthorizedSessionError("Authentication is required")
        return LockedUsers(actor, None, current, (current,))

    async def lock_users(
        self,
        *,
        access: UserAccess,
        target_id: UUID | None,
        include_target_sessions: bool,
        read_only_actor: bool = False,
    ) -> LockedUsers:
        """認証時の actor を信用せず、原会話と必要な全 row を固定順で読み直す。"""

        if read_only_actor and (target_id is not None or include_target_sessions):
            raise ValueError("Read-only actor locking cannot include target users or sessions")
        await lock_organization(self._session, access.actor.organization_id)
        user_ids = {access.actor.user_id}
        if target_id is not None:
            user_ids.add(target_id)
        users = (
            await self._session.scalars(
                select(User)
                .where(
                    User.organization_id == access.actor.organization_id,
                    User.id.in_(sorted(user_ids)),
                )
                .order_by(User.id)
                # 調度認領は Schedule lock 後に User FK の KEY SHARE を取得する。
                # 読取専用 actor は SHARE で失効/変更を防ぎ、逆順の FK 待機と両立させる。
                .with_for_update(read=read_only_actor)
                .execution_options(populate_existing=True)
            )
        ).all()
        by_id = {user.id: user for user in users}
        actor = by_id.get(access.actor.user_id)
        if actor is None:
            raise UnauthorizedSessionError("Authentication is required")
        token_hash = hash_session_secret(access.session_token)
        proof = (AuthSession.user_id == actor.id) & (AuthSession.token_hash == token_hash)
        predicate: ColumnElement[bool] = proof
        if include_target_sessions and target_id in by_id:
            predicate = or_(
                proof,
                (AuthSession.user_id == target_id) & AuthSession.revoked_at.is_(None),
            )
        sessions = tuple(
            (
                await self._session.scalars(
                    select(AuthSession).where(predicate).order_by(AuthSession.id).with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        current = next(
            (
                item
                for item in sessions
                if item.user_id == actor.id and item.token_hash == token_hash
            ),
            None,
        )
        if current is None:
            raise UnauthorizedSessionError("Authentication is required")
        return LockedUsers(
            actor, by_id.get(target_id) if target_id is not None else None, current, sessions
        )

    async def list_users(
        self, *, organization_id: UUID, query: str, limit: int, offset: int
    ) -> tuple[tuple[StoredUser, ...], int]:
        """先頭 page の client filter ではなく、同じ条件で全体件数と page を返す。"""

        predicate = User.organization_id == organization_id
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            predicate = predicate & or_(
                User.email.ilike(pattern, escape="\\"),
                User.display_name.ilike(pattern, escape="\\"),
            )
        total = await self._session.scalar(select(func.count()).select_from(User).where(predicate))
        users = (
            await self._session.scalars(
                select(User)
                .where(predicate)
                .order_by(User.email, User.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return tuple(self.to_stored(user) for user in users), int(total or 0)

    async def email_exists(self, *, organization_id: UUID, email: str) -> bool:
        """組織 lock 中に同じ正規化 login identity の既存 row を調べる。"""

        return (
            await self._session.scalar(
                select(User.id).where(User.organization_id == organization_id, User.email == email)
            )
        ) is not None

    async def has_other_active_admin(self, user: User) -> bool:
        """全管理経路が共有する組織 lock の中で最後の ADMIN を判定する。"""

        return (
            await self._session.scalar(
                select(User.id)
                .where(
                    User.organization_id == user.organization_id,
                    User.id != user.id,
                    User.system_role == "ADMIN",
                    User.status == "ACTIVE",
                )
                .limit(1)
            )
        ) is not None

    async def insert_user(self, user: User) -> None:
        """FK 子の監査を追加する前に親 User を DB に保存する。"""

        self._session.add(user)
        await self._session.flush()

    def append_event(
        self,
        *,
        user: User,
        actor_id: UUID,
        request_id: UUID,
        action: UserSecurityAction,
        previous_role: str | None,
        previous_status: str | None,
        revoked_sessions: int,
        now: datetime,
    ) -> None:
        """自由入力を受けず、User と同じ transaction に監査を追加する。"""

        if not isinstance(request_id, UUID):
            raise ValueError("Security audit requires a server request UUID")
        self._session.add(
            UserSecurityEvent(
                id=uuid4(),
                organization_id=user.organization_id,
                user_id=user.id,
                actor_id=actor_id,
                action=action.value,
                row_version=user.row_version,
                previous_role=previous_role,
                previous_status=previous_status,
                system_role=user.system_role,
                status=user.status,
                revoked_sessions=revoked_sessions,
                request_id=request_id,
                created_at=now,
            )
        )

    async def list_events(
        self, *, organization_id: UUID, user_id: UUID, limit: int, offset: int
    ) -> tuple[tuple[StoredUserSecurityEvent, ...], int]:
        """組織と対象 user の双方で監査範囲を固定し、最近の操作から返す。"""

        predicate = (UserSecurityEvent.organization_id == organization_id) & (
            UserSecurityEvent.user_id == user_id
        )
        total = await self._session.scalar(
            select(func.count()).select_from(UserSecurityEvent).where(predicate)
        )
        events = (
            await self._session.scalars(
                select(UserSecurityEvent)
                .where(predicate)
                .order_by(UserSecurityEvent.created_at.desc(), UserSecurityEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return tuple(self.event_to_stored(event) for event in events), int(total or 0)

    @staticmethod
    def to_stored(user: User) -> StoredUser:
        """明示許可した account field だけを外へ出す。"""

        return StoredUser(
            user.id,
            user.email,
            user.display_name,
            UserRole(user.system_role),
            UserStatus(user.status),
            user.row_version,
            user.created_at,
            user.updated_at,
        )

    @staticmethod
    def event_to_stored(event: UserSecurityEvent) -> StoredUserSecurityEvent:
        """内部 ORM metadata を公開せず、列挙した監査 field を返す。"""

        return StoredUserSecurityEvent(
            event.id,
            event.user_id,
            event.actor_id,
            UserSecurityAction(event.action),
            event.row_version,
            UserRole(event.previous_role) if event.previous_role else None,
            UserStatus(event.previous_status) if event.previous_status else None,
            UserRole(event.system_role),
            UserStatus(event.status),
            event.revoked_sessions,
            event.request_id,
            event.created_at,
        )
