"""conftest の module 名に依存しないユーザー用例の共有型と clock。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

from projectmind.db.models import AuthSession, User
from projectmind.users.domain import UserAccess
from projectmind.users.service import UserService

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
PASSWORD = "test-only original password"


class Clock(datetime):
    """実 clock を待たず、lock/計算の前後で時刻の変化を再現する。"""

    current = NOW

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        """Fixture が指定した現在時刻を返す。"""

        return cls.current


@dataclass
class UserHarness:
    """実 domain/model と mock query を分け、実 DB の証拠と誤認させない。"""

    service: UserService
    repository: Mock
    session: Mock
    transaction: AsyncMock
    factory: Mock
    actor: User
    target: User
    current: AuthSession
    other_sessions: tuple[AuthSession, ...]
    access: UserAccess
    added: list[object]
    order: list[str]
