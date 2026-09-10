"""Project 用例の SQL fake と現在 credential の観測対象を共有する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from skillmind.db.models import Project, ProjectMember, ProjectMemberEvent
from skillmind.projects.service import ProjectService
from skillmind.users.domain import UserAccess
from skillmind.users.repository import LockedUsers


@dataclass
class Members:
    """SQL/transaction は mock と明示し、変更対象と実認証 model を観察可能にする。"""

    service: ProjectService
    session: MagicMock
    transaction: MagicMock
    access: UserAccess
    locked: LockedUsers
    project: Project
    lock_users: AsyncMock

    def member(self, status: str = "ACTIVE") -> ProjectMember:
        """再追加前の joined_at が新しい変更時刻より前になる固定 row を作る。"""

        assert self.locked.target is not None
        before = datetime.now(UTC) - timedelta(days=1)
        return ProjectMember(
            id=uuid4(), project_id=self.project.id, user_id=self.locked.target.id,
            status=status, joined_at=before, created_at=before, updated_at=before,
        )

    def queries(self, member: ProjectMember | None) -> None:
        """Project と Member の lock query 結果のみを差し替える。"""

        self.session.scalar.side_effect = [self.project, member]

    def events(self) -> list[ProjectMemberEvent]:
        """実 repository が同じ session に追加した監査だけを取り出す。"""

        return [
            call.args[0] for call in self.session.add.call_args_list
            if isinstance(call.args[0], ProjectMemberEvent)
        ]
