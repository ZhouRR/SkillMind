"""普通 Run 作成 transaction に内部認領の検証と結算を参加させる。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.runs.creation_request import TaskRunIntent
from skillmind.runs.domain import CreatedRun


@dataclass(frozen=True, slots=True)
class RunCreationAuthority:
    """同じ作成 transaction で再検証した現在の権限を保持する。"""

    actor_system_role: str
    project_membership: str


@runtime_checkable
class RunCreationParticipant(Protocol):
    """Run/hash/event を再実装せず、内部 use case の認領だけを束縛する。"""

    async def authorize(
        self, session: AsyncSession, *, intent: TaskRunIntent, idempotency_key: str
    ) -> RunCreationAuthority:
        """現在の身分と元の要求を検証し、結算まで必要な DB lock を保持する。"""

        ...

    async def before_create(self, session: AsyncSession) -> None:
        """元の Run が存在しない場合だけ、新規作成を許可するか再検証する。"""

        ...

    async def complete(self, session: AsyncSession, created: CreatedRun) -> None:
        """初期 snapshot の凍結後、同じ transaction で元の認領へ関連付ける。"""

        ...
