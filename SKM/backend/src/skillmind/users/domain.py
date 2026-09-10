"""アカウント管理の明示 DTO と競合・拒否理由を定義する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from skillmind.auth.domain import normalize_email, validate_password
from skillmind.auth.service import AuthenticatedActor


class UserRole(StrEnum):
    """既存の二つの system role だけを公開する。"""

    ADMIN = "ADMIN"
    USER = "USER"


class UserStatus(StrEnum):
    """無効化しても user や監査履歴を削除しない。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class UserSecurityAction(StrEnum):
    """自由記述 metadata を持たない監査の操作名。"""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    SESSIONS_REVOKED = "SESSIONS_REVOKED"


class UserNotFoundError(LookupError):
    """不存在と他組織の対象を区別せず拒否する。"""


class UserVersionConflictError(RuntimeError):
    """表示後に別の管理変更が提交されたことを示す。"""


class UserEmailConflictError(RuntimeError):
    """同じ組織の正規化 email を二つの user に割り当てない。"""


class LastActiveAdminError(RuntimeError):
    """最後の活動 ADMIN を失う role/status 変更を拒否する。"""


class UserAdministrationDeniedError(RuntimeError):
    """管理 transaction 内で ADMIN 権限が無いことを示す。"""


class CurrentPasswordRejectedError(RuntimeError):
    """本人の現在 password を確認できず、hash と会話を変更しない。"""


@dataclass(frozen=True, slots=True)
class UserAccess:
    """入口 actor と再検証用 credential を束ね、repr で secret を漏らさない。"""

    actor: AuthenticatedActor
    request_id: UUID
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CreateUserCommand:
    """初期 password は作成時だけ使用し、保存/応答 DTO へ渡さない。"""

    email: str
    display_name: str
    system_role: UserRole
    password: str = field(repr=False)

    def validated(self) -> CreateUserCommand:
        """HTTP 以外の caller にも同じ正規化と長さ制約を適用する。"""

        email = normalize_email(self.email)
        if len(email) > 320:
            raise ValueError("Email must not exceed 320 characters")
        validate_password(self.password)
        return CreateUserCommand(
            email=email,
            display_name=validate_display_name(self.display_name),
            system_role=UserRole(self.system_role),
            password=self.password,
        )


@dataclass(frozen=True, slots=True)
class UpdateUserCommand:
    """完全な編集内容と基にした版をまとめ、部分 field の誤消去を避ける。"""

    display_name: str
    system_role: UserRole
    status: UserStatus
    expected_row_version: int


@dataclass(frozen=True, slots=True)
class StoredUser:
    """Password/hash/内部 preference を除外した公開可能な account。"""

    user_id: UUID
    email: str
    display_name: str
    system_role: UserRole
    status: UserStatus
    row_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredUserSecurityEvent:
    """管理操作の最小監査投影。credential や自由入力を持たない。"""

    event_id: UUID
    user_id: UUID
    actor_id: UUID
    action: UserSecurityAction
    row_version: int
    previous_role: UserRole | None
    previous_status: UserStatus | None
    system_role: UserRole
    status: UserStatus
    revoked_sessions: int
    request_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UserMutationResult:
    """変更した account と、caller が再ログインすべきかを明示する。"""

    user: StoredUser
    revoked_sessions: int
    session_revoked: bool


def validate_display_name(value: str) -> str:
    """空白だけの表示名や保存上限を超える値を拒否する。"""

    value = value.strip()
    if not value or len(value) > 200:
        raise ValueError("Display name must contain 1 to 200 characters")
    return value


def validate_version(value: int) -> None:
    """bool や非正整数を楽観制御の版として扱わない。"""

    if type(value) is not int or value < 1:
        raise ValueError("Expected row version must be a positive integer")
