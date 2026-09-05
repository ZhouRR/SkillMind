"""首個 ADMIN を匿名 HTTP endpoint なしで初期化する use case。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.auth.domain import hash_password, normalize_email
from projectmind.db.models import Organization, User

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
    password: str


class BootstrapAdminService:
    """Organization lock 下で一度だけ初期 ADMIN を作成する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Transaction ごとの database session factory を保持する。"""

        self._session_factory = session_factory

    async def bootstrap(self, command: BootstrapAdminCommand) -> UUID:
        """既存 ADMIN がいない場合だけ ACTIVE ADMIN を追加する。"""

        email = normalize_email(command.email)
        display_name = command.display_name.strip()
        if not display_name:
            raise ValueError("Display name is required")
        password_hash = hash_password(command.password)

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
            session.add(
                User(
                    id=user_id,
                    organization_id=SYSTEM_ORGANIZATION_ID,
                    email=email,
                    password_hash=password_hash,
                    display_name=display_name,
                    system_role="ADMIN",
                    status="ACTIVE",
                    last_login_at=None,
                )
            )
            return user_id
