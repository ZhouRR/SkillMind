"""User 管理の授権、版、失効、監査と失敗の伝播を用例境界で確認する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.auth.domain import verify_password
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import User, UserSecurityEvent
from skillmind.users.domain import (
    CreateUserCommand,
    CurrentPasswordRejectedError,
    LastActiveAdminError,
    UpdateUserCommand,
    UserAdministrationDeniedError,
    UserEmailConflictError,
    UserNotFoundError,
    UserRole,
    UserStatus,
    UserVersionConflictError,
)
from tests.users.user_harness import NOW, PASSWORD, Clock, UserHarness


@pytest.mark.asyncio
async def test_exact_user_read_uses_the_locked_target_without_mutation(users: UserHarness) -> None:
    """ADMIN の再確認で一覧を検索せず、対象版/会話/監査を変えない。"""

    result = await users.service.get_user(access=users.access, user_id=users.target.id)
    assert result.user_id == users.target.id and result.row_version == 1
    users.repository.lock_users.assert_awaited_once_with(
        access=users.access, target_id=users.target.id, include_target_sessions=False
    )
    users.repository.list_users.assert_not_called()
    assert not users.added and all(item.revoked_at is None for item in users.other_sessions)


@pytest.mark.asyncio
async def test_exact_user_read_revalidates_role_and_missing_target(users: UserHarness) -> None:
    """入口の ADMIN snapshot では足りず、原会話と lock 後の役割で拒否する。"""

    with pytest.raises(UserNotFoundError):
        await users.service.get_user(access=users.access, user_id=uuid4())
    users.actor.system_role = "USER"
    users.current.system_role_at_login = "USER"
    with pytest.raises(UserAdministrationDeniedError):
        await users.service.get_user(access=users.access, user_id=users.target.id)
    assert not users.added


def command(users: UserHarness, **changes: object) -> UpdateUserCommand:
    """現在の target を基に一つの変更点だけを作る。"""

    return replace(
        UpdateUserCommand(
            users.target.display_name,
            UserRole(users.target.system_role),
            UserStatus(users.target.status),
            users.target.row_version,
        ),
        **changes,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,revoked",
    [
        ({"display_name": "New name"}, 0),
        ({"status": UserStatus.DISABLED}, 2),
        ({"system_role": UserRole.ADMIN}, 2),
    ],
)
async def test_changes_version_revoke_and_audit_together(
    users: UserHarness, changes: dict[str, object], revoked: int
) -> None:
    """表示名と security 変更を分け、期限切れでも未撤销なら同じ失効対象にする。"""

    result = await users.service.update_user(
        access=users.access, user_id=users.target.id, command=command(users, **changes)
    )
    assert result.user.row_version == 2
    assert result.revoked_sessions == revoked
    assert not result.session_revoked
    assert users.current.revoked_at is None
    event = users.added[0]
    assert isinstance(event, UserSecurityEvent)
    assert (event.user_id, event.actor_id, event.row_version, event.revoked_sessions) == (
        users.target.id,
        users.actor.id,
        2,
        revoked,
    )
    assert event.previous_role == "USER" and event.previous_status == "ACTIVE"
    assert event.request_id == users.access.request_id
    users.transaction.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_noop_checks_version_but_does_not_create_event(users: UserHarness) -> None:
    """同一資料でも古い版は conflict、正しい版だけを no-op とする。"""

    with pytest.raises(UserVersionConflictError):
        await users.service.update_user(
            access=users.access,
            user_id=users.target.id,
            command=command(users, expected_row_version=2),
        )
    result = await users.service.update_user(
        access=users.access, user_id=users.target.id, command=command(users)
    )
    assert result.user.row_version == 1 and not users.added


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"system_role": UserRole.USER}, {"status": UserStatus.DISABLED}]
)
async def test_last_active_admin_cannot_be_lost(
    users: UserHarness, changes: dict[str, object]
) -> None:
    """管理用例が最後の活動 ADMIN を検査し、失敗では変更を適用しない。"""

    users.target.system_role = "ADMIN"
    users.repository.has_other_active_admin.return_value = False
    with pytest.raises(LastActiveAdminError):
        await users.service.update_user(
            access=users.access, user_id=users.target.id, command=command(users, **changes)
        )
    assert users.target.row_version == 1 and users.target.status == "ACTIVE"
    assert all(item.revoked_at is None for item in users.other_sessions)
    assert not users.added


@pytest.mark.asyncio
async def test_reenable_never_resurrects_revoked_sessions(users: UserHarness) -> None:
    """状態を戻しても revoked_at を消さず、二つの操作として記録する。"""

    await users.service.update_user(
        access=users.access,
        user_id=users.target.id,
        command=command(users, status=UserStatus.DISABLED),
    )
    result = await users.service.update_user(
        access=users.access,
        user_id=users.target.id,
        command=command(users, status=UserStatus.ACTIVE),
    )
    assert result.user.row_version == 3 and result.revoked_sessions == 0
    assert all(item.revoked_at == NOW for item in users.other_sessions)
    assert len(users.added) == 2


@pytest.mark.asyncio
async def test_explicit_revoke_records_even_zero_and_self_revoke_invalidates_caller(
    users: UserHarness,
) -> None:
    """失効の操作事実を保持し、本人操作後の cookie は次の認証で拒否する。"""

    for item in users.other_sessions:
        item.revoked_at = NOW
    result = await users.service.revoke_sessions(
        access=users.access, user_id=users.target.id, expected_row_version=1
    )
    assert result.revoked_sessions == 0 and result.user.row_version == 2
    result = await users.service.revoke_sessions(
        access=users.access, user_id=users.actor.id, expected_row_version=1
    )
    assert result.session_revoked and result.revoked_sessions == 1
    with pytest.raises(UnauthorizedSessionError):
        await users.service.get_account(access=users.access)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_password", ["test-only next password", "new pass"])
async def test_password_change_uses_real_hash_and_revokes_current_session(
    users: UserHarness, new_password: str,
) -> None:
    """現 password を実 verifier で確認してから更新し、現在会話も持久失効させる。"""

    with pytest.raises(CurrentPasswordRejectedError):
        await users.service.change_password(
            access=users.access,
            current_password="incorrect",
            new_password=new_password,
            expected_row_version=1,
        )
    assert users.current.revoked_at is None and not users.added
    result = await users.service.change_password(
        access=users.access,
        current_password=PASSWORD,
        new_password=new_password,
        expected_row_version=1,
    )
    assert result.session_revoked and result.user.row_version == 2
    assert verify_password(users.actor.password_hash, new_password)[0]
    assert not verify_password(users.actor.password_hash, PASSWORD)[0]
    assert users.added[0].action == "PASSWORD_CHANGED"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["expiry", "revocation", "role", "status"])
async def test_credentials_are_revalidated_after_lock_wait(
    users: UserHarness, failure: str
) -> None:
    """古い actor で入場できても、lock 後に変化した会話/役割/期限では変更しない。"""

    original = users.repository.lock_users.side_effect

    async def waited(**kwargs: object) -> object:
        """Lock 待機の完了時点で別 transaction による変化を返す。"""

        result = await original(**kwargs)
        if failure == "expiry":
            Clock.current = NOW + timedelta(hours=9)
        elif failure == "revocation":
            users.current.revoked_at = NOW
        elif failure == "role":
            users.actor.system_role = "USER"
        else:
            users.actor.status = "DISABLED"
        return result

    users.repository.lock_users.side_effect = waited
    with pytest.raises(UnauthorizedSessionError):
        await users.service.update_user(
            access=users.access,
            user_id=users.target.id,
            command=command(users, display_name="New name"),
        )
    assert not users.added


@pytest.mark.asyncio
async def test_password_computation_is_followed_by_fresh_authorization(
    users: UserHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash 計算中に期限を越えた要求を、計算前の時刻で許可しない。"""

    def late_hash(password: str) -> str:
        """実待機をせず、計算完了時には期限切れになった状況を作る。"""

        Clock.current = NOW + timedelta(hours=9)
        return users.actor.password_hash

    monkeypatch.setattr("skillmind.users.service.hash_password", late_hash)
    with pytest.raises(UnauthorizedSessionError):
        await users.service.create_user(
            access=users.access,
            command=CreateUserCommand("new@example.test", "New", UserRole.USER, PASSWORD),
        )
    assert not users.added


@pytest.mark.asyncio
async def test_admin_role_csrf_and_target_boundaries(users: UserHarness) -> None:
    """内部用例にも現権限と CSRF の検証があり、target 不存在を同じ error にする。"""

    with pytest.raises(CsrfRejectedError):
        await users.service.revoke_sessions(
            access=replace(users.access, csrf_token="wrong"),
            user_id=users.target.id,
            expected_row_version=1,
        )
    with pytest.raises(UserNotFoundError):
        await users.service.revoke_sessions(
            access=users.access, user_id=uuid4(), expected_row_version=1
        )
    users.actor.system_role = users.current.system_role_at_login = "USER"
    with pytest.raises(UserAdministrationDeniedError):
        await users.service.create_user(
            access=users.access,
            command=CreateUserCommand("new@example.test", "New", UserRole.USER, PASSWORD),
        )
    assert not users.added


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [PASSWORD, "new pass"])
async def test_creation_flushes_parent_before_event_and_hides_password(
    users: UserHarness, password: str,
) -> None:
    """FK 順序と公開 DTO の非秘密性を、DB default に依存せず確認する。"""

    input = CreateUserCommand(" New@Example.test ", " New ", UserRole.USER, password)
    result = await users.service.create_user(access=users.access, command=input)
    assert users.order == ["User", "flush", "UserSecurityEvent", "flush"]
    assert result.user.email == "new@example.test" and result.user.display_name == "New"
    assert result.user.row_version == 1
    assert isinstance(users.added[0], User)
    assert users.added[1].action == "CREATED"
    assert verify_password(users.added[0].password_hash, password)[0]
    assert password not in repr(input) + repr(result)
    assert users.access.session_token not in repr(users.access)
    assert users.access.csrf_token not in repr(users.access)


@pytest.mark.asyncio
async def test_duplicate_email_is_not_a_password_overwrite(users: UserHarness) -> None:
    """同じ identity の新規要求を既存 user の更新に変換しない。"""

    users.repository.email_exists.return_value = True
    with pytest.raises(UserEmailConflictError):
        await users.service.create_user(
            access=users.access,
            command=CreateUserCommand("new@example.test", "New", UserRole.USER, PASSWORD),
        )
    assert not users.added


@pytest.mark.asyncio
async def test_flush_failure_leaves_through_transaction_error_path(users: UserHarness) -> None:
    """監査失敗を transaction へ伝播する。実 DB rollback は別途検証する。"""

    failure = RuntimeError("test-only flush rejection")
    users.session.flush.side_effect = failure
    with pytest.raises(RuntimeError, match="flush"):
        await users.service.revoke_sessions(
            access=users.access, user_id=users.target.id, expected_row_version=1
        )
    assert users.transaction.__aexit__.await_args.args[:2] == (RuntimeError, failure)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (25, -1), (25, False)])
async def test_invalid_page_is_rejected_before_database(
    users: UserHarness, limit: int, offset: int
) -> None:
    """無限 page と bool coercion を内部 caller にも許可しない。"""

    with pytest.raises(ValueError):
        await users.service.list_users(access=users.access, limit=limit, offset=offset)
    users.factory.assert_not_called()


@pytest.mark.asyncio
async def test_lists_revalidate_and_preserve_server_paging(users: UserHarness) -> None:
    """検索/page を repository へ渡し、読み取り中に期限を越えた結果を返さない。"""

    expected = (users.repository.to_stored(users.target),)
    users.repository.list_users = AsyncMock(return_value=(expected, 501))
    assert await users.service.list_users(
        access=users.access, query=" name ", limit=25, offset=100
    ) == (expected, 501)
    users.repository.list_users.assert_awaited_once_with(
        organization_id=users.actor.organization_id, query="name", limit=25, offset=100
    )

    async def late_events(**kwargs: object) -> tuple[tuple[object, ...], int]:
        """長い query の後に現在時刻が更新されることを確認する。"""

        Clock.current = NOW + timedelta(hours=9)
        return (), 0

    users.repository.list_events = AsyncMock(side_effect=late_events)
    with pytest.raises(UnauthorizedSessionError):
        await users.service.list_security_events(
            access=users.access, user_id=users.actor.id, limit=25, offset=0
        )
