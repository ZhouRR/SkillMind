"""首個 ADMIN を匿名 HTTP endpoint なしで初期化する use case。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.auth.domain import hash_password
from skillmind.db.models import Organization, User
from skillmind.users.domain import CreateUserCommand, UserRole, UserSecurityAction
from skillmind.users.repository import UserRepository

SYSTEM_ORGANIZATION_ID = UUID("00000000-0000-4000-8000-000000000100")


class AdminAlreadyExistsError(RuntimeError):
    """初期 ADMIN が既に存在し、bootstrap 再実行を拒否したことを示す。"""


class BootstrapOrganizationNotFoundError(RuntimeError):
    """Migration が作る Organization boundary が存在しないことを示す。"""


@dataclass(frozen=True, slots=True)
class BootstrapAdminCommand:
    """CLI から bootstrap use case へ渡す初期 ADMIN 情報。"""

    email: str
    display_name: str
    password: str = field(repr=False)


class BootstrapAdminService:
    """Organization lock 下で一度だけ初期 ADMIN を作成する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Transaction ごとの database session factory を保持する。"""

        self._session_factory = session_factory

    async def bootstrap(self, command: BootstrapAdminCommand) -> UUID:
        """既存 ADMIN がいない場合だけ ACTIVE ADMIN を追加する。"""

        validated = CreateUserCommand(
            command.email, command.display_name, UserRole.ADMIN, command.password
        ).validated()
        password_hash = await asyncio.to_thread(hash_password, validated.password)

        async with self._session_factory() as session, session.begin():
            # 単一 Organization row を排他 lock し、並行 CLI が二人の初期 ADMIN を作るのを防ぐ。
            organization = (
                await session.scalars(
                    select(Organization)
                    .where(Organization.id == SYSTEM_ORGANIZATION_ID)
                    .with_for_update()
                )
            ).one_or_none()
            if organization is None:
                raise BootstrapOrganizationNotFoundError(
                    "Run database migrations before bootstrapping the first administrator"
                )
            existing_admin = (
                await session.scalars(
                    select(User.id)
                    .where(
                        User.organization_id == SYSTEM_ORGANIZATION_ID,
                        User.system_role == "ADMIN",
                    )
                    .limit(1)
                )
            ).first()
            if existing_admin is not None:
                raise AdminAlreadyExistsError("An administrator already exists")

            user_id = uuid4()
            now = datetime.now(UTC)
            user = User(
                id=user_id,
                organization_id=SYSTEM_ORGANIZATION_ID,
                email=validated.email,
                password_hash=password_hash,
                display_name=validated.display_name,
                system_role="ADMIN",
                status="ACTIVE",
                last_login_at=None,
                row_version=1,
                created_at=now,
                updated_at=now,
            )
            repository = UserRepository(session)
            # CLI は HTTP request を捏造せず、操作 UUID と新 ADMIN 自身を監査主体にする。
            await repository.insert_user(user)
            repository.append_event(
                user=user,
                actor_id=user_id,
                request_id=uuid4(),
                action=UserSecurityAction.CREATED,
                previous_role=None,
                previous_status=None,
                revoked_sessions=0,
                now=now,
            )
            await session.flush()
            return user_id
