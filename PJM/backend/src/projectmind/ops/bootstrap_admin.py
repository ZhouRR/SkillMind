"""Interactive terminal から首個 ADMIN を安全に作成する。"""

from __future__ import annotations

import asyncio
import getpass

from projectmind.auth.bootstrap import (
    AdminAlreadyExistsError,
    BootstrapAdminCommand,
    BootstrapAdminService,
)
from projectmind.core.settings import Settings
from projectmind.db.resources import create_database_engine, create_session_factory


async def bootstrap_admin(command: BootstrapAdminCommand) -> str:
    """Application 設定の PostgreSQL に初期 ADMIN を作成して ID を返す。"""

    settings = Settings()
    engine = create_database_engine(settings)
    try:
        service = BootstrapAdminService(create_session_factory(engine))
        return str(await service.bootstrap(command))
    finally:
        await engine.dispose()


def read_command() -> BootstrapAdminCommand:
    """Password を argv や shell history に残さず terminal から読み取る。"""

    email = input("Admin email: ").strip()
    display_name = input("Display name: ").strip()
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    return BootstrapAdminCommand(email=email, display_name=display_name, password=password)


def main() -> int:
    """初期化結果だけを出力し、credential や database URL は表示しない。"""

    try:
        user_id = asyncio.run(bootstrap_admin(read_command()))
    except (AdminAlreadyExistsError, ValueError) as error:
        print(f"Bootstrap refused: {error}")
        return 1
    print(f"Initial ADMIN created: {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
