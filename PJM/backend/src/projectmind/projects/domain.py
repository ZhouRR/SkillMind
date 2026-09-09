"""Project metadata と membership の公開 use case 型を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

MAX_PROJECT_VERSION = 2_147_483_647


def validate_project_version(value: object) -> int:
    """bool や小数を版とみなさず、DB と同じ閉区間だけを受け付ける。"""

    if type(value) is not int or not 1 <= value <= MAX_PROJECT_VERSION:
        raise ValueError("Project version must be an integer from 1 to 2147483647")
    return value


def require_project_version(actual: int, expected: int) -> None:
    """no-op や削除条件より前に、利用者が確認した元の版との一致を要求する。"""

    validate_project_version(expected)
    if actual != expected:
        raise ProjectVersionConflictError("Project version changed")


def next_project_version(current: int) -> int:
    """実変更だけが一度増版し、整数上限で巻き戻らない。"""

    validate_project_version(current)
    if current == MAX_PROJECT_VERSION:
        raise ProjectVersionExhaustedError("Project version is exhausted")
    return current + 1


class ProjectVersionConflictError(RuntimeError):
    """表示した元の版と、持鎖して取得した版が異なる。"""


class ProjectVersionExhaustedError(RuntimeError):
    """保持済みの最大版を越える変更を、部分適用せず拒否する。"""


class ProjectStatus(StrEnum):
    """Project の利用可否を表す永続状態。"""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectMemberStatus(StrEnum):
    """User と Project の有効な所属関係を表す。"""

    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


class ProjectMemberAction(StrEnum):
    """所属関係の実変更だけを記録する監査操作。"""

    ADDED = "ADDED"
    REMOVED = "REMOVED"


class ProjectNotFoundError(RuntimeError):
    """Project が存在しないか actor から参照できない場合の domain error。"""


class ProjectArchivedError(RuntimeError):
    """参照権限はあるが、帰档済み Project への書き込みは許可されない。"""


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

    Run と監査記録は削除で復元できないため、ARCHIVED でない、または Run/Schedule/所属監査が残る
    Project は key を解放するためであっても消させない。``blockers`` は安定した公開拒否へ
    対応する識別子であり、参照を削除して制約を回避してよいという指示ではない。
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

    expected_row_version: int
    name: str | None = None
    description: str | None = None
    settings: dict[str, Any] | None = None
    retention_days: int | None = None

    def __post_init__(self) -> None:
        """内部 caller も版だけ/全 null の PATCH を変更として受け付けない。"""

        validate_project_version(self.expected_row_version)
        if all(value is None for value in (
            self.name, self.description, self.settings, self.retention_days,
        )):
            raise ValueError("At least one non-null project field must be provided")


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
    row_version: int
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
