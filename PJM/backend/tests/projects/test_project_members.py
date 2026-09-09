"""実 credential/用例と fake transaction で所属変更の拒否・監査境界を検証する。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.db.models import AuthSession, Project, ProjectMember, ProjectMemberEvent, User
from projectmind.projects.domain import ProjectMemberNotFoundError, ProjectMemberUserNotFoundError
from projectmind.projects.repository import ProjectRepository
from projectmind.projects.service import ProjectService
from projectmind.users.domain import UserAccess, UserAdministrationDeniedError
from projectmind.users.repository import LockedUsers, UserRepository


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


@pytest.fixture
def members(monkeypatch: pytest.MonkeyPatch) -> Members:
    """外部サービスを使わず、原 cookie と照合可能な現在 ADMIN/対象 User を用意する。"""

    now = datetime.now(UTC)
    organization_id = uuid4()
    actor = User(
        id=uuid4(), organization_id=organization_id, email="admin@example.test",
        display_name="Admin", password_hash="unused", system_role="ADMIN", status="ACTIVE",
        row_version=1, created_at=now, updated_at=now,
    )
    target = User(
        id=uuid4(), organization_id=organization_id, email="member@example.test",
        display_name="Member", password_hash="unused", system_role="USER", status="ACTIVE",
        row_version=1, created_at=now, updated_at=now,
    )
    credentials = generate_session_credentials()
    current = AuthSession(
        id=uuid4(), user_id=actor.id, token_hash=credentials.session_token_hash,
        csrf_token_hash=credentials.csrf_token_hash, credential_version=2,
        system_role_at_login="ADMIN", idle_expires_at=now + timedelta(minutes=30),
        absolute_expires_at=now + timedelta(hours=8), revoked_at=None,
        created_at=now, last_seen_at=now,
    )
    access = UserAccess(
        AuthenticatedActor(actor.id, organization_id, actor.email, actor.display_name, "ADMIN"),
        uuid4(), credentials.session_token, credentials.csrf_token,
    )
    project = Project(
        id=uuid4(), organization_id=organization_id, key="members", name="Members",
        description="", status="ACTIVE", settings_json={}, retention_days=90,
        created_at=now, updated_at=now,
    )
    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction = session.begin.return_value
    transaction.__aexit__.return_value = False
    locked = LockedUsers(actor, target, current, (current,))
    lock_users = AsyncMock(return_value=locked)
    monkeypatch.setattr(UserRepository, "lock_users", lock_users)
    return Members(
        ProjectService(MagicMock(return_value=session)), session, transaction,
        access, locked, project, lock_users,
    )


@pytest.mark.asyncio
async def test_first_add_records_actor_request_and_complete_before_after_state(
    members: Members,
) -> None:
    """新規所属を先に flush し、同一 session の監査へ原 actor/request と前後を保存する。"""

    members.queries(None)
    target = members.locked.target
    assert target is not None
    result = await members.service.add_member(
        access=members.access, project_id=members.project.id, user_id=target.id,
    )

    event, = members.events()
    added_member = members.session.add.call_args_list[0].args[0]
    assert isinstance(added_member, ProjectMember)
    assert event.member_id == added_member.id
    assert event.actor_id == members.access.actor.user_id
    assert event.request_id == members.access.request_id
    assert event.project_id == members.project.id and event.user_id == target.id
    assert event.organization_id == members.access.actor.organization_id
    assert event.action == "ADDED" and event.status == "ACTIVE"
    assert event.previous_status is None and event.previous_joined_at is None
    assert event.joined_at == result.joined_at == event.created_at
    members.lock_users.assert_awaited_once_with(
        access=members.access, target_id=target.id, include_target_sessions=False,
    )
    assert members.session.flush.await_count == 2
    assert [call[0] for call in members.session.mock_calls if call[0] in {"add", "flush"}] == [
        "add", "flush", "add", "flush",
    ]
    members.transaction.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["ACTIVE", "REMOVED"])
async def test_repeated_add_does_not_fabricate_events_but_readd_appends(
    members: Members, previous: str,
) -> None:
    """旧 ACTIVE は時刻も監査も不変、REMOVED は同じ row の前後を一件追加する。"""

    member = members.member(previous)
    before = member.joined_at
    members.queries(member)
    result = await members.service.add_member(
        access=members.access, project_id=members.project.id, user_id=member.user_id,
    )

    assert result.status == "ACTIVE"
    if previous == "ACTIVE":
        assert result.joined_at == before == member.updated_at
        members.session.add.assert_not_called()
    else:
        event, = members.events()
        assert event.member_id == member.id and event.previous_status == "REMOVED"
        assert event.previous_joined_at == before and event.joined_at > before
        assert event.status == "ACTIVE" and event.action == "ADDED"
        assert member.created_at == before


@pytest.mark.asyncio
@pytest.mark.parametrize("target_status", ["ACTIVE", "DISABLED"])
async def test_remove_archived_membership_records_event_without_changing_account(
    members: Members, target_status: str,
) -> None:
    """Archive/無効 account の古い所属も解除でき、account role/status は操作しない。"""

    members.project.status = "ARCHIVED"
    assert members.locked.target is not None
    members.locked.target.status = target_status
    member = members.member()
    members.queries(member)

    await members.service.remove_member(
        access=members.access, project_id=members.project.id, user_id=member.user_id,
    )

    event, = members.events()
    assert event.action == "REMOVED" and event.previous_status == "ACTIVE"
    assert event.status == member.status == "REMOVED"
    assert event.joined_at == event.previous_joined_at == member.joined_at
    assert members.locked.target.status == target_status
    assert members.locked.target.system_role == "USER"
    members.session.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["REMOVED", None])
async def test_missing_or_already_removed_membership_is_not_found_without_event(
    members: Members, state: str | None,
) -> None:
    """解除の反復を成功に見せず、存在しない変更履歴も作らない。"""

    assert members.locked.target is not None
    member = members.member(state) if state else None
    members.queries(member)
    with pytest.raises(ProjectMemberNotFoundError):
        await members.service.remove_member(
            access=members.access, project_id=members.project.id,
            user_id=members.locked.target.id,
        )
    members.session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_exists", [True, False])
async def test_locked_disabled_or_outside_organization_target_cannot_be_added(
    members: Members, target_exists: bool,
) -> None:
    """Org/User lock の結果が無効/不可視なら、同じ User not found で拒否する。"""

    assert members.locked.target is not None
    user_id = members.locked.target.id
    members.locked.target.status = "DISABLED"
    if not target_exists:
        members.lock_users.return_value = replace(members.locked, target=None)
    members.queries(None)
    with pytest.raises(ProjectMemberUserNotFoundError):
        await members.service.add_member(
            access=members.access, project_id=members.project.id, user_id=user_id,
        )
    members.session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["expired", "revoked", "disabled", "role_changed", "csrf", "wrong_actor"],
)
async def test_member_lock_wait_is_followed_by_original_credential_recheck(
    members: Members, change: str,
) -> None:
    """Project/Member 待機中の失効を再現し、待機前の認証成功だけでは変更させない。"""

    calls = 0
    assert members.locked.target is not None

    async def scalar(statement: object) -> Project | None:
        """二つ目の資源 lock が戻る瞬間に、現状態の差を注入する。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return members.project
        if change == "expired":
            members.locked.current_session.idle_expires_at = (
                datetime.now(UTC) - timedelta(seconds=1)
            )
        elif change == "revoked":
            members.locked.current_session.revoked_at = datetime.now(UTC)
        elif change == "disabled":
            members.locked.actor.status = "DISABLED"
        elif change == "role_changed":
            members.locked.actor.system_role = "USER"
        elif change == "csrf":
            members.locked.current_session.csrf_token_hash = "invalid"
        else:
            members.locked.actor.organization_id = uuid4()
        return None

    members.session.scalar.side_effect = scalar
    expected = UnauthorizedSessionError
    with pytest.raises(expected):
        await members.service.add_member(
            access=members.access, project_id=members.project.id,
            user_id=members.locked.target.id,
        )
    members.session.add.assert_not_called()
    members.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_non_admin_session_cannot_reuse_stale_admin_actor(members: Members) -> None:
    """有効な USER 会話であっても、古い入口 ADMIN object を権限として使わない。"""

    members.locked.actor.system_role = "USER"
    members.locked.current_session.system_role_at_login = "USER"
    with pytest.raises(UserAdministrationDeniedError):
        await members.service.list_members(access=members.access, project_id=members.project.id)
    members.session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_csrf_is_validated_again_in_member_transaction(members: Members) -> None:
    """入口 fake が通しても、原 CSRF が一致しない書込は資源 lock 前に拒否する。"""

    assert members.locked.target is not None
    with pytest.raises(CsrfRejectedError):
        await members.service.add_member(
            access=replace(members.access, csrf_token="invalid-test-csrf"),
            project_id=members.project.id, user_id=members.locked.target.id,
        )
    members.session.scalar.assert_not_awaited()
    members.session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["audit_flush", "expiry_after_flush"])
async def test_audit_failure_or_late_expiry_exits_whole_transaction_with_error(
    members: Members, failure_at: str,
) -> None:
    """実 rollback は主張せず、親 INSERT/監査が同じ失敗 transaction 内にあることを固定する。"""

    members.queries(None)
    assert members.locked.target is not None
    flush_count = 0
    failure = RuntimeError("test audit insert rejected")

    async def flush() -> None:
        """親 INSERT の次の監査 flush の失敗/期限越えを差し込む。"""

        nonlocal flush_count
        flush_count += 1
        if flush_count == 2:
            if failure_at == "audit_flush":
                raise failure
            members.locked.current_session.absolute_expires_at = (
                datetime.now(UTC) - timedelta(seconds=1)
            )

    members.session.flush.side_effect = flush
    expected = RuntimeError if failure_at == "audit_flush" else UnauthorizedSessionError
    with pytest.raises(expected) as raised:
        await members.service.add_member(
            access=members.access, project_id=members.project.id,
            user_id=members.locked.target.id,
        )
    assert len(members.events()) == 1
    assert members.transaction.__aexit__.call_args.args[:2] == (expected, raised.value)
    members.session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_list_rechecks_original_session_after_query(
    members: Members, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一覧は pagination/account 状態を発明せず、query 後の期限切れなら結果を公開しない。"""

    async def list_members(self: ProjectRepository, **kwargs: object) -> tuple[()]:
        """読み取りが終わる時点の失効を再現する。"""

        members.locked.current_session.revoked_at = datetime.now(UTC)
        return ()

    monkeypatch.setattr(ProjectRepository, "list_members", list_members)
    with pytest.raises(UnauthorizedSessionError):
        await members.service.list_members(access=members.access, project_id=members.project.id)
    members.session.add.assert_not_called()
