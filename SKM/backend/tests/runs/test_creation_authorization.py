"""普通 Run の全 return が原会話を再認証することを実 repository 付きで検証する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.elements import ColumnElement

from skillmind.auth.domain import generate_session_credentials, hash_session_secret
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.db.models import (
    AuthSession,
    Organization,
    OutboxMessage,
    Project,
    ProjectMember,
    ProjectSkillVersion,
    ResourceBinding,
    Run,
    RunEvent,
    RunSegment,
    RunSkillSnapshot,
    SkillVersion,
    User,
)
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.domain import IdempotencyConflictError, RunStatus, TaskSourceSelectionError
from tests.runs.creation_authorization_harness import (
    PATHS,
    CreationAuthorizationHarness,
    row_values,
)
from tests.runs.creation_fakes import creation_command, stored_creation


class CreationClock:
    """期限値を変えず、lock/flush 待機後の時計だけを進める。"""

    current = datetime.now(UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        """元 timezone を保った合成時刻を返す。"""

        return cls.current.astimezone(tz)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> type[CreationClock]:
    """経過を fake 時刻に限定し、実際に長時間 lock を待った証拠と混同しない。"""

    CreationClock.current = datetime.now(UTC)
    monkeypatch.setattr("skillmind.runs.service.datetime", CreationClock)
    return CreationClock


def invalidate(db: CreationAuthorizationHarness, failure: str) -> type[Exception]:
    """原会話と現在 Project への独立した拒否を、共有 validator の入力として作る。"""

    if failure == "revoked":
        db.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "disabled":
        db.user.status = "DISABLED"
    elif failure == "role":
        db.user.system_role = "ADMIN"
    elif failure == "role-aba":
        db.user.system_role = "USER"
        db.auth_session.revoked_at = datetime.now(UTC)
    elif failure == "csrf":
        db.access = replace(db.access, csrf_token="synthetic-invalid-csrf")
        return CsrfRejectedError
    elif failure == "missing-session":
        db.auth_sessions.clear()
    elif failure == "missing-user":
        db.users.clear()
    elif failure == "missing-org":
        db.organization = None
    elif failure == "session-owner":
        db.auth_session.user_id = uuid4()
    elif failure == "old-credentials":
        db.auth_session.credential_version = 1
    elif failure == "member":
        assert db.member is not None
        db.member.status = "REMOVED"
        return ProjectNotFoundError
    elif failure == "missing-project":
        db.project_present = False
        return ProjectNotFoundError
    elif failure == "cross-org":
        db.project.organization_id = uuid4()
        return ProjectNotFoundError
    elif failure == "archived":
        db.project.status = "ARCHIVED"
        return ProjectArchivedError
    else:
        raise AssertionError(f"Unknown failure: {failure}")
    return UnauthorizedSessionError


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_every_creation_return_authenticates_and_waits_for_commit(path: str) -> None:
    """flush/DTO は成功回执ではなく、原 transaction の正常退出まで待つ。"""

    db = CreationAuthorizationHarness(path)
    winner_before = row_values(db.winner)
    identities = deepcopy([row_values(db.user), row_values(db.auth_session)])
    db.commit_release = asyncio.Event()
    task = asyncio.create_task(db.call())
    try:
        await asyncio.wait_for(db.commit_entered.wait(), timeout=2)
        assert not task.done() and db.commits == 0
        db.commit_release.set()
        result = await asyncio.wait_for(task, timeout=2)
    finally:
        db.commit_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert db.transactions == db.commits == 1
    assert db.rollbacks == 0 and not db.staged
    assert [row_values(db.user), row_values(db.auth_session)] == identities
    assert row_values(db.winner) == winner_before
    assert db.events[-2:] == ["flush", "commit"]
    if path == "find-miss":
        assert result is None and db.committed == []
    elif path == "new":
        assert result is not None and not result.idempotent_replay
        assert result.status is RunStatus.QUEUED
        assert [type(row) for row in db.committed] == [
            Run,
            RunSegment,
            RunEvent,
            OutboxMessage,
            RunSkillSnapshot,
        ]
        created = db.committed[0]
        assert created.permission_snapshot_json["actor_id"] == str(db.user.id)
        assert created.permission_snapshot_json["actor_system_role"] == "USER"
        assert created.permission_snapshot_json["project_membership"] == "ACTIVE"
    else:
        assert result is not None and result.idempotent_replay
        assert result.run_id == db.winner.id and result.row_version == 7
        assert result.status is RunStatus.SUCCEEDED
        assert db.committed == [db.winner]
        db.session.add_all.assert_not_called()
        assert "skill" not in db.events and "manifest" not in db.events
    if path not in {"new", "source-winner", "unique-winner"}:
        db.session.execute.assert_not_awaited()
    if path in {"first-replay", "find-hit", "find-miss"}:
        assert "source" not in db.events
    if path == "source-winner":
        assert db.events.count("lookup") == 2 and "source" in db.events
    if path == "unique-winner":
        assert db.events.count("lookup") == 2 and db.session.execute.await_count == 1


@pytest.mark.asyncio
async def test_creation_uses_shared_lock_order_and_refreshes_loaded_identities() -> None:
    """FK と両立する User/Project SHARE と原 Session UPDATE を実 SQL で確認する。"""

    db = CreationAuthorizationHarness()
    await db.call()
    queries = [
        query for query in db.statements if getattr(query, "_for_update_arg", None) is not None
    ]
    expected = [
        Organization,
        User,
        AuthSession,
        Project,
        ProjectMember,
        SkillVersion,
        ProjectSkillVersion,
    ]
    assert [query.column_descriptions[0]["entity"] for query in queries] == expected
    dialect_factory: Callable[..., Dialect] = postgresql.dialect
    for query, entity in zip(queries, expected, strict=True):
        sql = str(query.compile(dialect=dialect_factory()))
        if entity is SkillVersion:
            assert sql.endswith("FOR SHARE OF skill_versions")
            assert query.get_execution_options()["populate_existing"] is True
            continue
        assert sql.endswith(
            "FOR SHARE"
            if entity in {User, Project, ProjectMember, ProjectSkillVersion}
            else "FOR UPDATE"
        )
        if entity is not Organization:
            assert query.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("failure", ["revoked", "member", "archived"])
async def test_all_entry_paths_reject_lost_access_before_key_lookup(
    path: str, failure: str
) -> None:
    """原 Run があっても、失効した caller が key を使って存在を照会できない。"""

    db = CreationAuthorizationHarness(path)
    before = [row_values(row) for row in db.committed]
    error = invalidate(db, failure)
    with pytest.raises(error):
        await db.call()
    assert "lookup" not in db.events
    assert [row_values(row) for row in db.committed] == before
    assert not db.staged and db.commits == 0
    assert db.transactions == db.rollbacks == 1
    db.session.execute.assert_not_awaited()
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "disabled",
        "role",
        "role-aba",
        "csrf",
        "missing-session",
        "missing-user",
        "missing-org",
        "session-owner",
        "old-credentials",
        "cross-org",
    ],
)
async def test_current_credential_validation_is_not_an_entry_actor_stub(failure: str) -> None:
    """共用 UserRepository/validator に実 credential 状態を渡し、認証 mock で迂回しない。"""

    db = CreationAuthorizationHarness("find-hit")
    error = invalidate(db, failure)
    with pytest.raises(error):
        await db.call()
    assert "lookup" not in db.events and db.committed == [db.winner]
    assert db.commits == 0 and db.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "find-hit"])
async def test_browser_actor_argument_cannot_replace_original_access_actor(path: str) -> None:
    """別 actor ID を原会話へ添えても、他人の要求を作成/確認させない。"""

    db = CreationAuthorizationHarness(path)
    db.intent = replace(db.intent, actor_id=uuid4())
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert db.transactions == 0 and db.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("current_admin", [False, True])
async def test_frozen_permission_uses_locked_user_instead_of_stale_entry_role(
    current_admin: bool,
) -> None:
    """現在 role と login snapshot の一致後、古い facade の role を権限へ戻さない。"""

    db = CreationAuthorizationHarness()
    role = "ADMIN" if current_admin else "USER"
    db.user.system_role = role
    db.auth_session.system_role_at_login = role
    db.access = replace(
        db.access,
        actor=replace(
            db.access.actor,
            system_role="USER" if current_admin else "ADMIN",
        ),
    )
    if current_admin:
        db.member = None
    await db.call()
    run = next(row for row in db.committed if isinstance(row, Run))
    assert run.permission_snapshot_json["actor_system_role"] == role
    assert run.permission_snapshot_json["project_membership"] == (
        "ADMIN_BYPASS" if current_admin else "ACTIVE"
    )
    assert ("lock:ProjectMember" in db.events) is not current_admin


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_original", [False, True])
async def test_same_actor_new_login_cannot_substitute_for_original_session(
    remove_original: bool,
) -> None:
    """新 login の存在だけでは原 cookie を救済せず、新 credential の明示要求だけを認める。"""

    db = CreationAuthorizationHarness("find-hit")
    credentials = generate_session_credentials()
    newer = AuthSession(
        **{
            **row_values(db.auth_session),
            "id": uuid4(),
            "token_hash": credentials.session_token_hash,
            "csrf_token_hash": credentials.csrf_token_hash,
        }
    )
    db.auth_sessions.append(newer)
    if remove_original:
        db.auth_sessions.remove(db.auth_session)
    else:
        db.auth_session.revoked_at = datetime.now(UTC)
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert "lookup" not in db.events
    queried = [
        query for query in db.statements if query.column_descriptions[0]["entity"] is AuthSession
    ]
    assert len(queried) == 1
    assert queried[0].compile().params["token_hash_1"] == hash_session_secret(
        db.access.session_token
    )
    # Run の原 actor は同一。現在の明示 login で確認しても、過去 snapshot は更新しない。
    before = row_values(db.winner)
    db.access = replace(
        db.access, session_token=credentials.session_token, csrf_token=credentials.csrf_token
    )
    result = await db.call()
    assert result is not None and result.run_id == db.winner.id
    assert row_values(db.winner) == before
    db.session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "find-miss"])
@pytest.mark.parametrize(
    "stage", ["Organization", "User", "AuthSession", "Project", "ProjectMember"]
)
async def test_lock_wait_checks_expiry_before_lookup_or_source_resolution(
    path: str,
    stage: str,
    clock: type[CreationClock],
) -> None:
    """同じ期限でも、指定 lock の待機後に到達した現在時刻で失効を判定する。"""

    db = CreationAuthorizationHarness(path)
    deadline = clock.current + timedelta(seconds=1)
    db.auth_session.idle_expires_at = deadline

    def expire(name: str) -> None:
        """lock が返る時点の経過時間だけを合成する。"""

        if name == f"lock:{stage}":
            clock.current = deadline

    db.on_step = expire
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert db.auth_session.idle_expires_at == deadline
    assert "lookup" not in db.events and "source" not in db.events
    assert db.committed == [] and not db.staged and db.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing-project", "cross-org"])
async def test_missing_project_lock_result_still_rechecks_expired_original_session(
    failure: str,
    clock: type[CreationClock],
) -> None:
    """Project helper が先に 404 を投げても、待機中の失効を旧資格で隠さない。"""

    db = CreationAuthorizationHarness("find-hit")
    invalidate(db, failure)
    deadline = clock.current + timedelta(seconds=1)
    db.auth_session.absolute_expires_at = deadline

    def expire(name: str) -> None:
        """見つからない Project SELECT が返った時点で期限へ達する。"""

        if name == "lock:Project":
            clock.current = deadline

    db.on_step = expire
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert "lookup" not in db.events and db.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_final_flush_expiry_rolls_back_new_rows_but_not_external_winner(
    path: str,
    clock: type[CreationClock],
) -> None:
    """全 success/lookup miss の出口で失効を検知し、他 transaction の勝者を削除しない。"""

    db = CreationAuthorizationHarness(path)
    before = row_values(db.winner)
    deadline = clock.current + timedelta(seconds=1)
    db.auth_session.absolute_expires_at = deadline
    staged_at_flush: list[Any] = []

    def expire(name: str) -> None:
        """最後の FK/flush 待機で保存候補が揃った後に期限を越える。"""

        if name == "flush":
            staged_at_flush.extend(db.staged)
            clock.current = deadline

    db.on_step = expire
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert db.events[-1] == "flush"
    assert db.rollbacks == 1 and db.commits == 0 and not db.staged
    assert row_values(db.winner) == before
    if path == "new":
        assert [type(row) for row in staged_at_flush] == [
            Run,
            RunSegment,
            RunEvent,
            OutboxMessage,
            RunSkillSnapshot,
        ]
    assert db.committed == ([] if path in {"new", "find-miss"} else [db.winner])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source", "idempotency"])
@pytest.mark.parametrize("lost_access", [False, True])
async def test_known_business_rejection_reauthenticates_before_rollback(
    kind: str,
    lost_access: bool,
) -> None:
    """既知 422/409 の経路でも、途中で失効した本人へ旧資格の業務理由を返さない。"""

    db = CreationAuthorizationHarness("source-miss" if kind == "source" else "first-replay")
    if kind == "idempotency":
        db.winner.permission_snapshot_json["actor_id"] = str(uuid4())
    before = row_values(db.winner)

    def revoke(name: str) -> None:
        """source の拒否または original hash 比較が返る直前に会話を失効させる。"""

        if lost_access and name == ("source" if kind == "source" else "lookup"):
            invalidate(db, "revoked")

    db.on_step = revoke
    error = (
        UnauthorizedSessionError
        if lost_access
        else (TaskSourceSelectionError if kind == "source" else IdempotencyConflictError)
    )
    with pytest.raises(error):
        await db.call()
    assert db.rollbacks == 1 and db.commits == 0 and not db.staged
    assert row_values(db.winner) == before
    db.session.execute.assert_not_awaited()
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["source-winner", "unique-winner"])
async def test_winner_discovered_after_revocation_cannot_escape_final_authorization(
    path: str,
) -> None:
    """勝者の原 snapshot が正しくても、資源/INSERT 待機中の撤権を成功で隠さない。"""

    db = CreationAuthorizationHarness(path)

    def revoke(name: str) -> None:
        """本物 resolver/ON CONFLICT の応答が返る時点に失効を注入する。"""

        if name == ("source" if path == "source-winner" else "insert"):
            invalidate(db, "revoked")

    db.on_step = revoke
    before = row_values(db.winner)
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert db.events.count("lookup") == 2
    assert row_values(db.winner) == before and db.committed == [db.winner]
    assert not db.staged and db.commits == 0 and db.rollbacks == 1
    db.session.add_all.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "find-hit"])
@pytest.mark.parametrize("revoked", [False, True])
async def test_malformed_sources_are_normalized_only_after_current_authorization(
    path: str, revoked: bool
) -> None:
    """選択の構文拒否も、失効した原会話へ返す情報として使わせない。"""

    db = CreationAuthorizationHarness(path)
    db.sources = {"docs": "document:not-a-uuid"}
    if revoked:
        invalidate(db, "revoked")
    with pytest.raises(UnauthorizedSessionError if revoked else TaskSourceSelectionError):
        await db.call()
    assert "lookup" not in db.events and "insert" not in db.events
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "first-replay", "find-hit"])
async def test_waiting_caller_mutation_does_not_change_the_frozen_raw_request(path: str) -> None:
    """正規化を認証後に遅らせても、最初の await 前に取得した本文の copy を使う。"""

    db = CreationAuthorizationHarness(path)
    original = deepcopy(db.input_json)

    def mutate(name: str) -> None:
        """User lock 待機中に caller の元 dict を更新し、遅い参照捕捉を検出する。"""

        if name == "lock:User":
            db.input_json["positions"].append(99)
            db.sources["new-slot"] = "document:not-a-uuid"

    db.on_step = mutate
    result = await db.call()
    assert result is not None
    run = next(row for row in db.committed if isinstance(row, Run))
    assert run.input_json == original
    assert run.task_snapshot_json["creation_request"]["sources"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_commit_propagates_without_automatic_confirmation_or_new_insert(
    path: str,
    committed: bool,
) -> None:
    """commit 応答喪失は保存有無の両方を区別して合成し、success を返さない。"""

    db = CreationAuthorizationHarness(path)
    db.commit_unknown = committed
    with pytest.raises(ConnectionError, match="commit outcome unknown"):
        await db.call()
    assert db.transactions == 1 and db.session.begin.call_count == 1
    assert db.commits == int(committed) and db.rollbacks == int(not committed)
    assert not db.staged and not db.active
    assert db.events.count("commit") == 1
    if path == "new":
        assert len(db.committed) == (5 if committed else 0)
        db.session.execute.assert_awaited_once()
    elif path == "find-miss":
        assert db.committed == []
    else:
        assert db.committed == [db.winner]
        db.session.add_all.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "first-replay", "unique-winner"])
@pytest.mark.parametrize("stage", ["flush", "commit"])
async def test_cancelled_flush_or_commit_exit_does_not_return_a_prepared_success(
    path: str, stage: str
) -> None:
    """本 task の取消を伝播し、局部未 commit 行を捨てても外部勝者は残す。"""

    db = CreationAuthorizationHarness(path)
    entered, release = asyncio.Event(), asyncio.Event()
    if stage == "commit":
        entered = db.commit_entered
        db.commit_release = release
    else:

        async def wait_flush() -> None:
            """最終 flush の応答を待つ間に所有 task の cancel を配送する。"""

            db.step("flush")
            entered.set()
            await release.wait()

        db.session.flush.side_effect = wait_flush
    task = asyncio.create_task(db.call())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and not db.active and not db.staged
    assert db.transactions == db.rollbacks == 1 and db.commits == 0
    assert db.committed == ([] if path == "new" else [db.winner])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["first-replay", "unique-winner", "find-hit"])
async def test_legacy_original_hash_remains_read_only_under_current_authorization(
    path: str,
) -> None:
    """旧 hash の確認に現在の権限だけを要求し、旧 task/hash/snapshot を更新しない。"""

    db = CreationAuthorizationHarness(path, legacy=True)
    before = row_values(db.winner)
    if path != "unique-winner":
        db.version.status = "DISABLED"
    result = await db.call()
    assert result is not None and result.run_id == db.winner.id and result.idempotent_replay
    assert row_values(db.winner) == before
    assert "creation_request" not in db.winner.task_snapshot_json
    assert "skill" not in db.events and "manifest" not in db.events
    db.session.add_all.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_legacy_binding_after_hash_is_verified_without_live_resolution(corrupt: bool) -> None:
    """旧初期化で追加された binding だけを除外し、未知の過去を現在値で補わない。"""

    db = CreationAuthorizationHarness("find-hit")
    integration_id = uuid4()
    db.intent = replace(db.intent, sources={"repo": f"integration:{integration_id}"})
    db.sources = db.intent.sources
    command = replace(
        creation_command(db.intent, legacy=True),
        selected_sources_json={
            "repo": {
                "provider": "git",
                "capability": "repository.read/v1",
                "integration_id": str(integration_id),
                "candidate_key": f"integration:{integration_id}",
                "scope": {"paths": ["src/"]},
                "revision": "7",
                "source_binding_id": None,
            }
        },
    )
    db.winner = stored_creation(command)
    db.winner.selected_sources_json["repo"].update(
        {
            "binding_id": str(uuid4()),
            "binding_checksum": "sha256:stored",
            "binding_capability": "repository.read/v1",
        }
    )
    if corrupt:
        db.winner.selected_sources_json["repo"]["scope"] = {"paths": ["outside/"]}
    db.committed = [db.winner]
    before = row_values(db.winner)
    if corrupt:
        with pytest.raises(IdempotencyConflictError):
            await db.call()
    else:
        result = await db.call()
        assert result is not None and result.run_id == db.winner.id
    assert row_values(db.winner) == before
    assert "source" not in db.events and "skill" not in db.events
    db.session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", [User, AuthSession, Project, ProjectMember, Run])
async def test_authentication_and_key_lookup_fake_rejects_or_with_identical_bind_values(
    entity: type[Any],
) -> None:
    """実 SQL が OR に変わっても、fake の手書き AND が安全性を捏造しない。"""

    db = CreationAuthorizationHarness()
    predicates: list[ColumnElement[bool]]
    if entity is User:
        predicates = [User.organization_id == db.user.organization_id, User.id.in_([db.user.id])]
    elif entity is AuthSession:
        predicates = [
            AuthSession.user_id == db.user.id,
            AuthSession.token_hash == db.auth_session.token_hash,
        ]
    elif entity is Project:
        predicates = [
            Project.id == db.project.id,
            Project.organization_id == db.user.organization_id,
        ]
    elif entity is ProjectMember:
        predicates = [
            ProjectMember.project_id == db.project.id,
            ProjectMember.user_id == db.user.id,
        ]
    else:
        predicates = [
            Run.project_id == db.project.id,
            Run.task_id == db.winner.task_id,
            Run.idempotency_key == db.key,
        ]
    wrong = select(entity).where(or_(*predicates))
    assert wrong.compile().params == select(entity).where(and_(*predicates)).compile().params
    query = db.scalar if entity in {Project, ProjectMember} else db.scalars
    with pytest.raises(AssertionError):
        await query(wrong)


@pytest.mark.asyncio
async def test_fake_rejects_key_scope_omission_and_wrong_organization_operator() -> None:
    """別 operator/欠けた scope を同じ値だからという理由で受理しない。"""

    db = CreationAuthorizationHarness()
    with pytest.raises(AssertionError):
        await db.scalars(
            select(Run).where(Run.project_id == db.project.id, Run.idempotency_key == db.key)
        )
    with pytest.raises(AssertionError):
        await db.scalar(select(Organization).where(Organization.id != db.user.organization_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_real_source_freeze_and_initial_snapshot_share_final_authorization_transaction(
    expires: bool, clock: type[CreationClock]
) -> None:
    """成功した文書/Integration 凍結を含む全初期行を最終認証と一緒に commit/rollback する。"""

    db = CreationAuthorizationHarness(with_sources=True)
    assert db.document is not None
    source_before = [row_values(db.document), row_values(db.integration)]
    deadline = clock.current + timedelta(seconds=1)
    db.auth_session.absolute_expires_at = deadline
    initialized: list[Any] = []

    def observe(name: str) -> None:
        """binding の中間 flush でなく、source 回填後の最終 flush で失効を合成する。"""

        if name == "flush" and "initial-sources" in db.events:
            initialized.extend(db.staged)
            if expires:
                clock.current = deadline

    db.on_step = observe
    if expires:
        with pytest.raises(UnauthorizedSessionError):
            await db.call()
        assert db.committed == [] and db.rollbacks == 1 and db.commits == 0
    else:
        result = await db.call()
        assert result is not None and not result.idempotent_replay
        assert db.committed == initialized and db.commits == 1 and db.rollbacks == 0
    assert not db.staged and db.events.count("flush") == 2
    assert [type(row) for row in initialized] == [
        Run,
        RunSegment,
        RunEvent,
        OutboxMessage,
        RunSkillSnapshot,
        ResourceBinding,
    ]
    run, segment, event, outbox, snapshot, binding = initialized
    assert all(row.run_id == run.id for row in [segment, event, snapshot, binding])
    assert outbox.aggregate_id == run.id
    assert run.task_snapshot_json["creation_request"]["sources"] == db.intent.sources
    docs = run.selected_sources_json["docs"]["document_snapshot"]
    assert docs["documents"][0]["document_id"] == str(db.document.id)
    assert docs["documents"][0]["content_hash"] == db.document.checksum
    assert "storage_key" not in docs["documents"][0]
    frozen = run.selected_sources_json["repository-source"]
    assert frozen["binding_id"] == str(binding.id)
    assert frozen["binding_checksum"] == binding.checksum
    assert frozen["binding_capability"] == "repository.read/v1"
    assert binding.project_id == db.project.id and binding.created_by == db.user.id
    assert binding.integration_id == db.integration.id and binding.scope_level == "RUN"
    assert binding.scope_key == str(run.id) and binding.revision == "3"
    assert binding.scope_json == db.integration.scope_json
    assert [row_values(db.document), row_values(db.integration)] == source_before


@pytest.mark.asyncio
async def test_unique_winner_with_another_original_actor_is_rejected() -> None:
    """ON CONFLICT の勝者も原 identity を再比較し、別 actor の Run を成功返却しない。"""

    db = CreationAuthorizationHarness("unique-winner")
    db.winner.permission_snapshot_json["actor_id"] = str(uuid4())
    before = row_values(db.winner)
    with pytest.raises(IdempotencyConflictError):
        await db.call()
    assert db.events.count("lookup") == 2 and db.session.execute.await_count == 1
    assert db.committed == [db.winner] and row_values(db.winner) == before
    assert not db.staged and db.rollbacks == 1 and db.commits == 0
    db.session.add_all.assert_not_called()
    db.session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", [False, True])
@pytest.mark.parametrize("invalid", ["omitted", "none", "object"])
async def test_service_entry_cannot_revive_an_actor_only_authorization_bypass(
    lookup: bool, invalid: str
) -> None:
    """必須 authorization の省略/未定義実装を session factory より先に拒否する。"""

    db = CreationAuthorizationHarness()
    arguments: dict[str, Any] = {
        "project_id": db.project.id,
        "actor_id": db.user.id,
        "input_json": db.input_json,
        "sources": db.sources,
        "idempotency_key": db.key,
    }
    if invalid != "omitted":
        arguments["authorization"] = None if invalid == "none" else object()
    if lookup:
        arguments.update(
            skill_version_id=db.resolved.skill_version_id, task_key=db.resolved.task_key
        )
    else:
        arguments.update(resolved=db.resolved, trace_id="synthetic")
    with pytest.raises(TypeError):
        if lookup:
            await db.service.find_task_run_replay(**arguments)
        else:
            await db.service.create_task_run(**arguments)
    db.session_factory.assert_not_called()
    assert db.transactions == 0 and not db.statements and not db.events
