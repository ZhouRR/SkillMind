"""原会話の再認証と局部 transaction 境界を検証する。実 PostgreSQL の競争証明ではない。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from projectmind.auth.domain import generate_session_credentials
from projectmind.auth.service import AuthenticatedActor
from projectmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from projectmind.db.models import (
    AuthSession,
    InteractionResponse,
    Organization,
    Project,
    ProjectMember,
    Run,
    RunSegment,
    User,
    UserInteraction,
)
from projectmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from projectmind.runs.domain import InteractionExpiredError, RespondedInteraction
from projectmind.runs.service import RunService
from projectmind.users.domain import UserAccess
from tests.runs.test_interaction_lifecycle import InteractionDatabase


class AuthorizationTransaction:
    """例外時の行復元を局部再現し、service が commit/rollback のどちらを要求したか記録する。"""

    def __init__(self, database: AuthorizationDatabase) -> None:
        """DB 接続の代わりに明示的な model 集合を受け取る。"""

        self.database = database
        self.exception_type: type[BaseException] | None = None
        self.committed = False
        self.snapshots: list[tuple[Any, dict[str, Any]]] = []
        self.rows: list[Any] = []

    async def __aenter__(self) -> Self:
        """業務 model と追加行だけを snapshot し、認証側の時刻操作とは区別する。"""

        self.snapshots = [
            (
                row,
                deepcopy(
                    {key: value for key, value in vars(row).items() if key != "_sa_instance_state"}
                ),
            )
            for row in (self.database.run, self.database.segment, self.database.interaction)
        ]
        self.rows = list(self.database.rows)
        return self

    async def __aexit__(
        self, exception_type: type[BaseException] | None, exception: object, traceback: object
    ) -> None:
        """例外の外部伝播と fake の復元を観察し、実 DB の rollback と混同しない。"""

        del exception, traceback
        self.exception_type = exception_type
        if exception_type is not None or self.database.commit_error:
            for row, snapshot in self.snapshots:
                for key, value in snapshot.items():
                    setattr(row, key, value)
            self.database.rows[:] = self.rows
        else:
            self.committed = True
        if self.database.commit_error:
            raise RuntimeError("Synthetic commit failure")


class AuthorizationDatabase(InteractionDatabase):
    """本物の共有 validator/repository を使い、SQL 結果・待機・flush だけを fake にする。"""

    def __init__(self) -> None:
        """原 cookie に対応する ACTIVE User/Session/Project/所属を組み合わせる。"""

        super().__init__()
        now = datetime.now(UTC)
        self.organization = Organization(id=uuid4(), name="Synthetic organization")
        self.user = User(
            id=self.actor,
            organization_id=self.organization.id,
            email="reader@example.test",
            display_name="Reader",
            password_hash="unused",
            system_role="USER",
            status="ACTIVE",
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        credentials = generate_session_credentials()
        self.auth_session = AuthSession(
            id=uuid4(),
            user_id=self.user.id,
            token_hash=credentials.session_token_hash,
            csrf_token_hash=credentials.csrf_token_hash,
            credential_version=2,
            system_role_at_login="USER",
            revoked_at=None,
            created_at=now,
            last_seen_at=now,
            idle_expires_at=now + timedelta(minutes=30),
            absolute_expires_at=now + timedelta(hours=8),
        )
        self.access = UserAccess(
            actor=AuthenticatedActor(
                self.user.id, self.organization.id, self.user.email, self.user.display_name, "USER"
            ),
            request_id=uuid4(),
            session_token=credentials.session_token,
            csrf_token=credentials.csrf_token,
        )
        self.project = Project(
            id=self.run.project_id,
            organization_id=self.organization.id,
            key="ordinary",
            name="Ordinary",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.member: ProjectMember | None = ProjectMember(
            id=uuid4(),
            project_id=self.project.id,
            user_id=self.user.id,
            status="ACTIVE",
            joined_at=now,
            created_at=now,
            updated_at=now,
        )
        self.on_lock: Callable[[type[Any]], None] | None = None
        self.on_flush: Callable[[], None] | None = None
        self.commit_error = False
        self.transaction = AuthorizationTransaction(self)
        self.session.__aenter__.return_value = self.session
        self.session.begin.return_value = self.transaction
        self.session.scalar.side_effect = self.scalar
        self.session.flush.side_effect = self.flush
        self.service = RunService(MagicMock(return_value=self.session))

    async def scalar(self, statement: Select[Any]) -> Any:
        """Project 層の SQL scope を評価し、Run の sequence query は既存 fake に合わせる。"""

        entity = statement.column_descriptions[0]["entity"]
        if entity not in (Organization, Project, ProjectMember):
            return 20
        self.statements.append(statement)
        if self.on_lock:
            self.on_lock(entity)
        parameters = statement.compile().params
        if entity is Organization:
            return self.organization if self.organization.id in parameters.values() else None
        if entity is Project:
            return (
                self.project
                if self.project.organization_id in parameters.values()
                and self.project.id in parameters.values()
                else None
            )
        return self.member

    async def scalars(self, statement: Select[Any]) -> MagicMock:
        """共有 User/Session の lock 読取を処理し、Run 配下は実遷移付き fake に委譲する。"""

        entity = statement.column_descriptions[0]["entity"]
        if self.on_lock and statement._for_update_arg is not None:
            self.on_lock(entity)
        if entity not in (User, AuthSession):
            return await super().scalars(statement)
        self.statements.append(statement)
        result = MagicMock()
        result.all.return_value = [self.user if entity is User else self.auth_session]
        return result

    async def flush(self) -> None:
        """行を追加した後の待機時間や失敗を callback で再現する。"""

        if self.on_flush:
            self.on_flush()

    async def submit(self) -> RespondedInteraction:
        """actor_id を別入力にせず、原 credential 付きの公開 service 境界を呼ぶ。"""

        return await self.service.respond_to_interaction(
            access=self.access,
            project_id=self.project.id,
            run_id=self.run.id,
            interaction_id=self.interaction.id,
            interaction_version=1,
            response_json={"text": "  Keep the finding.  "},
            idempotency_key="response-original",
            trace_id=str(self.access.request_id),
        )

    async def prepare(self, mode: str) -> None:
        """旧回答の再確認または新規 expiry を同じ現在 credential で準備する。"""

        if mode == "replay":
            await self.respond()
        if mode in ("replay", "expired"):
            self.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    def invalidate(self, failure: str) -> None:
        """原会話と現在権限の独立した拒否を合成する。"""

        now = datetime.now(UTC)
        if failure == "revoked":
            self.auth_session.revoked_at = now
        elif failure == "disabled":
            self.user.status = "DISABLED"
        elif failure == "role":
            self.user.system_role = "ADMIN"
        elif failure == "role-aba":
            self.user.system_role = "USER"
            self.auth_session.revoked_at = now
        elif failure == "idle":
            self.auth_session.idle_expires_at = now - timedelta(seconds=1)
        elif failure == "absolute":
            self.auth_session.absolute_expires_at = now - timedelta(seconds=1)
        elif failure == "csrf":
            self.access = replace(self.access, csrf_token="wrong-original-proof")
        elif failure == "token":
            self.auth_session.token_hash = "not-the-original-session"
        elif failure == "removed":
            assert self.member is not None
            self.member.status = "REMOVED"
        elif failure == "missing-member":
            self.member = None
        elif failure == "cross-org":
            self.project.organization_id = uuid4()
        elif failure == "archived":
            self.project.status = "ARCHIVED"
        else:
            raise AssertionError(failure)


@pytest.mark.parametrize("mode", ["first", "replay", "expired"])
async def test_authorized_first_replay_and_expiry_use_same_locks_and_final_flush(mode: str) -> None:
    """三つの結果とも Org から全行を固定し、最後の flush 後にだけ commit する。"""

    database = AuthorizationDatabase()
    await database.prepare(mode)
    before = list(database.rows)
    database.statements.clear()
    if mode == "expired":
        with pytest.raises(InteractionExpiredError):
            await database.submit()
        assert database.interaction.status == "EXPIRED"
    else:
        result = await database.submit()
        assert result.idempotent_replay is (mode == "replay")
        response = next(row for row in database.rows if isinstance(row, InteractionResponse))
        assert response.actor_id == database.access.actor.user_id
        if mode == "replay":
            assert database.rows == before
    assert database.transaction.committed
    database.session.flush.assert_awaited_once()
    locked = [item for item in database.statements if item._for_update_arg is not None]
    assert [item.column_descriptions[0]["entity"] for item in locked] == [
        Organization,
        User,
        AuthSession,
        Project,
        ProjectMember,
        Run,
        RunSegment,
        UserInteraction,
    ]
    for statement in locked[1:]:
        assert statement.get_execution_options()["populate_existing"] is True
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if statement.column_descriptions[0]["entity"] in (Project, ProjectMember):
            assert sql.endswith("FOR SHARE") and "FOR UPDATE" not in sql
        else:
            assert sql.endswith("FOR UPDATE")


@pytest.mark.parametrize("mode", ["first", "replay", "expired"])
@pytest.mark.parametrize(
    "failure,error",
    [
        ("revoked", UnauthorizedSessionError),
        ("disabled", UnauthorizedSessionError),
        ("role", UnauthorizedSessionError),
        ("role-aba", UnauthorizedSessionError),
        ("idle", UnauthorizedSessionError),
        ("absolute", UnauthorizedSessionError),
        ("csrf", CsrfRejectedError),
        ("token", UnauthorizedSessionError),
        ("removed", ProjectNotFoundError),
        ("missing-member", ProjectNotFoundError),
        ("cross-org", ProjectNotFoundError),
        ("archived", ProjectArchivedError),
    ],
)
async def test_entry_actor_does_not_bypass_current_access(
    mode: str, failure: str, error: type[Exception]
) -> None:
    """入口 actor が古くても原会話・所属の現状を拒否し、expiry を先に確定しない。"""

    database = AuthorizationDatabase()
    await database.prepare(mode)
    before = list(database.rows)
    database.invalidate(failure)
    with pytest.raises(error):
        await database.submit()
    assert not database.transaction.committed and database.rows == before
    assert database.transaction.exception_type is error
    database.session.flush.assert_not_awaited()


@pytest.mark.parametrize("mode", ["first", "replay", "expired"])
@pytest.mark.parametrize("stage", ["user-lock", "project-lock", "run-lock", "flush"])
async def test_original_session_expiring_while_waiting_rolls_back_all_business_rows(
    mode: str, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """期限は入口の時刻で固定せず、待機後や保存後の失効が 410 より優先する。"""

    database = AuthorizationDatabase()
    await database.prepare(mode)
    initial = datetime.now(UTC)

    class Clock:
        """保存済み期限は変えず、持鎖待機を service の現在時刻だけに反映する。"""

        current = initial

        @staticmethod
        def now(timezone: object) -> datetime:
            """本物の wall clock 待機無しで、明示した検証時刻を返す。"""

            del timezone
            return Clock.current

    def advance() -> None:
        """既存 expiry の二秒後に進め、入口時刻の再利用では検出不能な失効を作る。"""

        Clock.current = initial + timedelta(seconds=3)

    monkeypatch.setattr("projectmind.runs.service.datetime", Clock)
    database.auth_session.idle_expires_at = initial + timedelta(seconds=1)
    before = (
        list(database.rows),
        database.run.status,
        database.run.row_version,
        database.segment.status,
        database.interaction.status,
        database.interaction.version,
    )
    if stage == "flush":
        database.on_flush = advance
    else:
        target = {"user-lock": User, "project-lock": Project, "run-lock": Run}[stage]
        database.on_lock = lambda entity: advance() if entity is target else None
    with pytest.raises(UnauthorizedSessionError):
        await database.submit()
    assert not database.transaction.committed
    assert database.transaction.exception_type is UnauthorizedSessionError
    assert (
        database.rows,
        database.run.status,
        database.run.row_version,
        database.segment.status,
        database.interaction.status,
        database.interaction.version,
    ) == before


@pytest.mark.parametrize("mode", ["first", "replay", "expired"])
@pytest.mark.parametrize(
    "failure,error",
    [
        ("revoked", UnauthorizedSessionError),
        ("disabled", UnauthorizedSessionError),
        ("role", UnauthorizedSessionError),
        ("removed", ProjectNotFoundError),
        ("archived", ProjectArchivedError),
    ],
)
async def test_final_gate_rechecks_locked_models_without_hiding_expiry(
    mode: str, failure: str, error: type[Exception]
) -> None:
    """持鎖行の人工変更で最終 validator を確認する。実 DB で同時更新できるという意味ではない。"""

    database = AuthorizationDatabase()
    await database.prepare(mode)
    before = list(database.rows)
    database.on_flush = lambda: database.invalidate(failure)
    with pytest.raises(error):
        await database.submit()
    assert database.rows == before and not database.transaction.committed
    assert database.transaction.exception_type is error
    database.session.flush.assert_awaited_once()


@pytest.mark.parametrize("mode", ["first", "replay", "expired"])
async def test_commit_or_flush_failure_is_not_confirmed_expiry(mode: str) -> None:
    """未知の commit/flush 失敗を確定した 410 や認証拒否に翻訳しない。"""

    for commit_failure in (True, False):
        database = AuthorizationDatabase()
        await database.prepare(mode)
        before = list(database.rows)
        if commit_failure:
            database.commit_error = True
        else:
            database.session.flush = AsyncMock(side_effect=RuntimeError("Synthetic flush failure"))
        with pytest.raises(RuntimeError, match="Synthetic"):
            await database.submit()
        assert database.rows == before and not database.transaction.committed
        if commit_failure:
            assert database.transaction.exception_type is None


async def test_current_role_controls_membership_bypass_not_entry_actor_role() -> None:
    """古い ADMIN 表示を信用せず、持鎖 User の USER 所属を要求する。"""

    database = AuthorizationDatabase()
    database.access = replace(
        database.access, actor=replace(database.access.actor, system_role="ADMIN")
    )
    database.member = None
    with pytest.raises(ProjectNotFoundError):
        await database.submit()
    assert database.rows == []


async def test_current_same_organization_admin_needs_no_member_row() -> None:
    """現在 ADMIN と原会話の role が一致するときだけ、同組織の所属検査を省く。"""

    database = AuthorizationDatabase()
    database.user.system_role = "ADMIN"
    database.auth_session.system_role_at_login = "ADMIN"
    database.member = None
    await database.submit()
    assert database.transaction.committed
    assert ProjectMember not in [
        item.column_descriptions[0]["entity"] for item in database.statements
    ]


async def test_removed_member_cannot_observe_archive_status() -> None:
    """帰档検査より前の共通 404 が、所属解除後の Project 情報を漏らさない。"""

    database = AuthorizationDatabase()
    database.invalidate("removed")
    database.invalidate("archived")
    with pytest.raises(ProjectNotFoundError):
        await database.submit()
    assert database.rows == []


async def test_missing_access_cannot_fall_back_to_an_actor_id() -> None:
    """Service の公開内部入口には credential 無しの互換経路を残さない。"""

    database = AuthorizationDatabase()
    with pytest.raises(TypeError, match="actor_id"):
        await database.service.respond_to_interaction(  # type: ignore[call-arg]
            actor_id=database.actor,
            project_id=database.project.id,
            run_id=database.run.id,
            interaction_id=database.interaction.id,
            interaction_version=1,
            response_json={"text": "Continue"},
            idempotency_key="original",
            trace_id=None,
        )
    assert database.statements == []
