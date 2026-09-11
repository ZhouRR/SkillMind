"""実 Run/認証 repository の SQL を局部処理し、外部勝者と未 commit 行を分離する。

lock 後の ORM 失効注入は防御的再検証の証拠であり、共通 lock に従う PostgreSQL writer が
同時更新できる証拠ではない。勝者の途中公開も repository の競争分岐に限る局部 seam。
取消テストは rollback を合成し、commit 済み取消の可能性は commit 未知の別分岐と区別する。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from sqlalchemy import Select, and_
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from skillmind.auth.domain import generate_session_credentials
from skillmind.auth.service import AuthenticatedActor
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    AuthSession,
    Integration,
    Organization,
    OutboxMessage,
    Project,
    ProjectDocument,
    ProjectMember,
    ProjectSkillVersion,
    ResourceBinding,
    Run,
    RunBudgetAccount,
    RunEvent,
    RunSegment,
    RunSkillSnapshot,
    RuntimeManifest,
    SkillVersion,
    User,
)
from skillmind.runs.domain import CreatedRun
from skillmind.runs.service import RunService
from skillmind.users.domain import UserAccess
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.runs.task_binding_fakes import TaskBindingRows
from tests.runs.test_task_run_service import _resolved

PATHS = ("new", "first-replay", "source-winner", "unique-winner", "find-hit", "find-miss")


def row_values(row: Any) -> dict[str, Any]:
    """ORM 内部 identity state を除き、不変行と rollback を比較する。"""

    return deepcopy({key: value for key, value in vars(row).items() if key != "_sa_instance_state"})


def require_where(
    statement: Select[Any], entity: type[Any], predicate: ColumnElement[bool]
) -> None:
    """bind 値だけの手書き AND で実 SQL の OR/!=/scope 欠落を隠さない。"""

    assert len(statement.column_descriptions) == 1
    assert statement.column_descriptions[0]["expr"] is entity
    assert len(statement.get_final_froms()) == 1
    assert statement.whereclause is not None and statement.whereclause.compare(predicate)


class CreationAuthorizationHarness:
    """本物の use case・認証・重放・INSERT をつなぎ、DB 応答だけを制御する。"""

    def __init__(
        self, path: str = "new", *, legacy: bool = False, with_sources: bool = False
    ) -> None:
        """原要求、現在資格、精確 Manifest と独立した transaction 観測を準備する。"""

        assert path in {*PATHS, "source-miss"}
        self.path = path
        self.document_id = uuid4()
        self.integration_id = uuid4()
        requirements: tuple[dict[str, object], ...] = (
            ()
            if path not in {"source-winner", "source-miss"} and not with_sources
            else (
                {
                    "key": "docs",
                    "kind": "document",
                    "required": True,
                    "access": "read",
                    "capabilities": ["document.read/v1"],
                },
            )
        )
        if with_sources:
            requirements += (
                {
                    "key": "repository-source",
                    "kind": "repository",
                    "required": True,
                    "access": "read",
                    "capabilities": ["repository.read/v1"],
                },
            )
        raw = _resolved(requirements)
        manifest = deepcopy(raw.skill_snapshot["manifest"])
        manifest_checksum = f"sha256:{sha256_hex(canonical_json(manifest))}"
        input_schema = {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "positions": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["target", "positions"],
            "additionalProperties": False,
        }
        self.resolved = replace(
            raw,
            manifest_checksum=manifest_checksum,
            input_schema=input_schema,
            input_schema_checksum=f"sha256:{sha256_hex(canonical_json(input_schema))}",
            skill_snapshot={
                **raw.skill_snapshot,
                "skill_version_id": str(raw.skill_version_id),
                "manifest_checksum": manifest_checksum,
                "manifest": manifest,
            },
        )
        sources = {"docs": f"document:{self.document_id}"} if requirements else {}
        if with_sources:
            sources["repository-source"] = f"integration:{self.integration_id}"
        self.intent = replace(
            creation_intent(sources=sources),
            skill_version_id=self.resolved.skill_version_id,
            task_key=self.resolved.task_key,
        )
        self.key = "original-request"
        self.input_json = self.intent.input_json
        self.sources = self.intent.sources
        now = datetime.now(UTC)
        self.organization: Organization | None = Organization(id=uuid4(), name="Synthetic org")
        self.user = User(
            id=self.intent.actor_id,
            organization_id=self.organization.id,
            email="creation@example.test",
            display_name="Creation reader",
            password_hash="unused",
            system_role="USER",
            status="ACTIVE",
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.users = [self.user]
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
        self.auth_sessions = [self.auth_session]
        self.access = UserAccess(
            actor=AuthenticatedActor(
                self.user.id,
                self.user.organization_id,
                self.user.email,
                self.user.display_name,
                "USER",
            ),
            request_id=uuid4(),
            session_token=credentials.session_token,
            csrf_token=credentials.csrf_token,
        )
        self.project = Project(
            id=self.intent.project_id,
            organization_id=self.user.organization_id,
            key="creation",
            name="Synthetic creation",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.project_present = True
        self.member: ProjectMember | None = ProjectMember(
            id=uuid4(),
            project_id=self.project.id,
            user_id=self.user.id,
            status="ACTIVE",
            joined_at=now,
            created_at=now,
            updated_at=now,
        )
        self.document = (
            ProjectDocument(
                id=self.document_id,
                project_id=self.project.id,
                folder="specs",
                name="original.md",
                storage_key="synthetic/not-read",
                size=16,
                mime="text/markdown",
                checksum=f"sha256:{'a' * 64}",
                uploaded_by=self.user.id,
                created_at=now,
            )
            if with_sources
            else None
        )
        self.integration = Integration(
            id=self.integration_id,
            project_id=self.project.id,
            name="Synthetic repository",
            kind="repository",
            provider="git",
            status="ACTIVE",
            revision=3,
            capabilities_json=["repository.read/v1"],
            scope_json={"repositories": ["synthetic/repository"]},
            config_json={},
            secret_reference_id=None,
            created_by=self.user.id,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self.version = SkillVersion(id=raw.skill_version_id, status="PUBLISHED")
        self.task_binding = TaskBindingRows(self.project, self.version)
        self.manifest = RuntimeManifest(
            id=uuid4(),
            skill_version_id=raw.skill_version_id,
            manifest_json=manifest,
            checksum=manifest_checksum,
        )
        self.winner = stored_creation(creation_command(self.intent, legacy=legacy))
        self.winner.status = "SUCCEEDED"
        self.winner.row_version = 7
        self.committed: list[Any] = [self.winner] if path in {"first-replay", "find-hit"} else []
        self.staged: list[Any] = []
        self.before: list[tuple[Any, dict[str, Any]]] = []
        self.active = False
        self.transactions = self.commits = self.rollbacks = 0
        self.commit_unknown: bool | None = None
        self.commit_entered = asyncio.Event()
        self.commit_release: asyncio.Event | None = None
        self.events: list[str] = []
        self.statements: list[Any] = []
        self.on_step: Callable[[str], None] | None = None
        self.session = MagicMock(spec=AsyncSession)
        self.session.__aenter__.return_value = self.session
        self.session.begin.side_effect = self.transaction
        self.session.scalar = AsyncMock(side_effect=self.scalar)
        self.session.scalars = AsyncMock(side_effect=self.scalars)
        self.session.execute = AsyncMock(side_effect=self.execute)
        self.session.get = AsyncMock(side_effect=self.get)
        self.session.flush = AsyncMock(side_effect=self.flush)
        self.session.add_all.side_effect = self.add_all
        self.session.add.side_effect = self.add
        self.session_factory = MagicMock(return_value=self.session)
        self.service = RunService(self.session_factory)

    def step(self, name: str) -> None:
        """SQL/flush の待機から返る時点だけに時刻・失効の注入を許す。"""

        assert self.active
        self.events.append(name)
        if self.on_step:
            self.on_step(name)

    def query(self, statement: Select[Any]) -> tuple[type[Any], dict[str, Any]]:
        """未知 SELECT の暗黙成功を避け、実 query と lock の発生順を記録する。"""

        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        if statement._for_update_arg is not None:
            self.step(f"lock:{entity.__name__}")
        return entity, statement.compile().params

    async def scalar(self, statement: Select[Any]) -> Any:
        """組織/Project/所属を実 SQL の完全な scope 条件で検索する。"""

        entity, params = self.query(statement)
        if entity in (SkillVersion, ProjectSkillVersion):
            if entity is SkillVersion:
                self.step("skill")
            return self.task_binding.scalar(statement)
        if entity is Organization:
            assert set(params) == {"id_1"}
            require_where(statement, entity, Organization.id == params["id_1"])
            return (
                self.organization
                if (self.organization is not None and self.organization.id == params["id_1"])
                else None
            )
        if entity is Project:
            if statement.column_descriptions[0]["expr"] is Project.organization_id:
                assert set(params) == {"id_1"}
                assert statement.whereclause is not None and statement.whereclause.compare(
                    Project.id == params["id_1"]
                )
                return (
                    self.project.organization_id
                    if (self.project_present and self.project.id == params["id_1"])
                    else None
                )
            assert set(params) == {"id_1", "organization_id_1"}
            require_where(
                statement,
                entity,
                and_(
                    Project.id == params["id_1"],
                    Project.organization_id == params["organization_id_1"],
                ),
            )
            return (
                self.project
                if (
                    self.project_present
                    and self.project.id == params["id_1"]
                    and self.project.organization_id == params["organization_id_1"]
                )
                else None
            )
        if entity is ProjectMember:
            assert set(params) == {"project_id_1", "user_id_1"}
            require_where(
                statement,
                entity,
                and_(
                    ProjectMember.project_id == params["project_id_1"],
                    ProjectMember.user_id == params["user_id_1"],
                ),
            )
            return (
                self.member
                if (
                    self.member is not None
                    and self.member.project_id == params["project_id_1"]
                    and self.member.user_id == params["user_id_1"]
                )
                else None
            )
        raise AssertionError(f"Unexpected scalar entity: {entity}")

    async def scalars(self, statement: Select[Any]) -> MagicMock:
        """原 User/Session、三元 Run identity、精確 Manifest 以外の SELECT を拒否する。"""

        entity, params = self.query(statement)
        rows: list[Any]
        if entity is User:
            assert set(params) == {"organization_id_1", "id_1"}
            require_where(
                statement,
                entity,
                and_(
                    User.organization_id == params["organization_id_1"],
                    User.id.in_(params["id_1"]),
                ),
            )
            rows = [
                user
                for user in self.users
                if (
                    user.organization_id == params["organization_id_1"]
                    and user.id in params["id_1"]
                )
            ]
        elif entity is AuthSession:
            assert set(params) == {"user_id_1", "token_hash_1"}
            require_where(
                statement,
                entity,
                and_(
                    AuthSession.user_id == params["user_id_1"],
                    AuthSession.token_hash == params["token_hash_1"],
                ),
            )
            rows = [
                session
                for session in self.auth_sessions
                if (
                    session.user_id == params["user_id_1"]
                    and session.token_hash == params["token_hash_1"]
                )
            ]
        elif entity is Run:
            assert set(params) == {"project_id_1", "task_id_1", "idempotency_key_1"}
            require_where(
                statement,
                entity,
                and_(
                    Run.project_id == params["project_id_1"],
                    Run.task_id == params["task_id_1"],
                    Run.idempotency_key == params["idempotency_key_1"],
                ),
            )
            self.step("lookup")
            rows = [
                row
                for row in [*self.committed, *self.staged]
                if (
                    isinstance(row, Run)
                    and row.project_id == params["project_id_1"]
                    and row.task_id == params["task_id_1"]
                    and row.idempotency_key == params["idempotency_key_1"]
                )
            ]
        elif entity is RuntimeManifest:
            assert set(params) == {"skill_version_id_1"}
            require_where(
                statement, entity, RuntimeManifest.skill_version_id == params["skill_version_id_1"]
            )
            self.step("manifest")
            rows = (
                [self.manifest]
                if self.manifest.skill_version_id == params["skill_version_id_1"]
                else []
            )
        elif entity is Integration:
            assert set(params) == {"id_1", "project_id_1"}
            require_where(
                statement,
                entity,
                and_(
                    Integration.id == params["id_1"],
                    Integration.project_id == params["project_id_1"],
                ),
            )
            self.step("integration")
            rows = (
                [self.integration]
                if (
                    self.integration.id == params["id_1"]
                    and self.integration.project_id == params["project_id_1"]
                )
                else []
            )
        else:
            raise AssertionError(f"Unexpected scalars entity: {entity}")
        result = MagicMock()
        result.all.return_value = rows
        assert len(rows) <= 1 or entity in {User, AuthSession}
        result.one_or_none.return_value = rows[0] if rows else None
        return result

    async def get(self, entity: type[Any], identity: UUID) -> Any:
        """新規作成の Version と文書 metadata だけを問い合わせる。"""

        if entity is SkillVersion:
            self.step("skill")
            return self.version if self.version.id == identity else None
        if entity is ProjectDocument:
            assert identity == self.document_id
            if self.path == "source-winner":
                self.publish_winner()
            self.step("source")
            return self.document
        if entity is Run:
            self.step("initial-sources")
            return next(
                (row for row in self.staged if isinstance(row, Run) and row.id == identity), None
            )
        raise AssertionError(f"Unexpected get entity: {entity}")

    async def execute(self, statement: Insert) -> MagicMock:
        """実 ON CONFLICT を観測し、競争勝者と自分の INSERT を混ぜない。"""

        assert isinstance(statement, Insert) and statement.table.name == "runs"
        sql = str(statement)
        assert "ON CONFLICT ON CONSTRAINT uq_runs_project_task_idempotency DO NOTHING" in sql
        assert sql.endswith("RETURNING runs.id")
        self.statements.append(statement)
        if self.path == "unique-winner":
            self.publish_winner()
        self.step("insert")
        values = statement.compile().params
        scope = (values["project_id"], values["task_id"], values["idempotency_key"])
        existing = [
            row
            for row in [*self.committed, *self.staged]
            if (
                isinstance(row, Run) and (row.project_id, row.task_id, row.idempotency_key) == scope
            )
        ]
        result = MagicMock()
        if existing:
            result.scalar_one_or_none.return_value = None
        else:
            row = Run(**deepcopy(values))
            self.staged.append(row)
            result.scalar_one_or_none.return_value = row.id
        return result

    def add(self, row: object) -> None:
        """初期 Segment/event/outbox/SkillSnapshot、予算と凍結 binding だけを受理する。"""

        assert self.active
        assert isinstance(
            row, RunSegment | RunEvent | OutboxMessage | RunSkillSnapshot | ResourceBinding
            | RunBudgetAccount
        )
        self.staged.append(row)

    def add_all(self, rows: list[Any]) -> None:
        """実 repository が生成した各行を同じ未 commit 集合へ追加する。"""

        for row in rows:
            self.add(row)

    async def flush(self) -> None:
        """最終認証の直前に生じる待機・拒否を合成する。"""

        self.step("flush")

    def publish_winner(self) -> None:
        """外部 transaction の勝者は、自分の rollback で削除しない。"""

        if self.winner not in self.committed:
            self.committed.append(self.winner)
            if self.active:
                self.before.append((self.winner, row_values(self.winner)))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """局部保存・rollback・commit 応答不明を分離し、成功返却の時点を観測する。"""

        assert not self.active and not self.staged
        self.active = True
        self.transactions += 1
        self.before = [(row, row_values(row)) for row in self.committed]
        try:
            try:
                yield
                self.step("commit")
                self.commit_entered.set()
                if self.commit_release is not None:
                    await self.commit_release.wait()
                if self.commit_unknown is False:
                    raise ConnectionError("Synthetic commit outcome unknown")
            except BaseException:
                self.rollbacks += 1
                for row, values in self.before:
                    for key in tuple(vars(row)):
                        if key != "_sa_instance_state" and key not in values:
                            delattr(row, key)
                    for key, value in values.items():
                        setattr(row, key, value)
                raise
            else:
                self.committed.extend(self.staged)
                self.commits += 1
                if self.commit_unknown is True:
                    raise ConnectionError("Synthetic commit outcome unknown")
        finally:
            self.staged.clear()
            self.active = False

    async def call(self) -> CreatedRun | None:
        """どの ordinary 入口も原 UserAccess を必須入力として実 service に渡す。"""

        common: dict[str, Any] = dict(
            project_id=self.intent.project_id,
            input_json=self.input_json,
            sources=self.sources,
            actor_id=self.intent.actor_id,
            idempotency_key=self.key,
            authorization=self.access,
        )
        if self.path in {"find-hit", "find-miss"}:
            return await self.service.find_task_run_replay(
                skill_version_id=self.resolved.skill_version_id,
                task_key=self.resolved.task_key,
                **common,
            )
        return await self.service.create_task_run(
            resolved=self.resolved, trace_id="synthetic", **common
        )
