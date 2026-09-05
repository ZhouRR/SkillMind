"""Project metadata と membership の公開 use case 型を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ProjectStatus(StrEnum):
    """Project の利用可否を表す永続状態。"""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectMemberStatus(StrEnum):
    """User と Project の有効な所属関係を表す。"""

    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class ProjectNotFoundError(RuntimeError):
    """Project が存在しないか actor から参照できない場合の domain error。"""


class ProjectKeyConflictError(RuntimeError):
    """Organization 内で Project key が重複した場合の domain error。"""


class ProjectPermissionDeniedError(RuntimeError):
    """Project 管理操作に必要な system role がない場合の domain error。"""


class ProjectMemberUserNotFoundError(RuntimeError):
    """同じ Organization の有効 User が見つからない場合の domain error。"""


class ProjectMemberNotFoundError(RuntimeError):
    """削除対象の有効 membership が見つからない場合の domain error。"""


class ProjectDeleteBlockedError(RuntimeError):
    """Project を物理削除できない状態を表す domain error。

    Run と監査記録は削除で復元できないため、ARCHIVED でない、または Run が残っている
    Project は key を解放するためであっても消させない。``blockers`` には利用者が自分で
    解消できる理由だけを載せる。
    """

    def __init__(self, message: str, *, blockers: tuple[str, ...] = ()) -> None:
        """理由文と、利用者へ提示する阻害要因の一覧を保持する。"""

        super().__init__(message)
        self.blockers = blockers


@dataclass(frozen=True, slots=True)
class CreateProjectCommand:
    """ADMIN が新規 Project を作成する入力。"""

    organization_id: UUID
    key: str
    name: str
    description: str
    settings: dict[str, Any]
    retention_days: int


@dataclass(frozen=True, slots=True)
class UpdateProjectCommand:
    """Project の変更可能な metadata だけを保持する入力。"""

    name: str | None = None
    description: str | None = None
    settings: dict[str, Any] | None = None
    retention_days: int | None = None


@dataclass(frozen=True, slots=True)
class StoredProject:
    """ORM 内部 field を公開しない Project read model。"""

    project_id: UUID
    key: str
    name: str
    description: str
    status: ProjectStatus
    settings: dict[str, Any]
    retention_days: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredProjectMember:
    """Credential を含まない Project membership read model。"""

    user_id: UUID
    email: str
    display_name: str
    status: ProjectMemberStatus
    joined_at: datetime


@dataclass(frozen=True, slots=True)
class StoredProjectPreference:
    """User が最後に選択した認可済み Project context。"""

    project_id: UUID | None
