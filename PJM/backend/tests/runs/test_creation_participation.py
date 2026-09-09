"""普通 Run 作成の全 return を内部認領と同じ transaction に束縛する回帰。

commit/rollback は局部 fake で観測し、実 PostgreSQL の競争や永続性は証明しない。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.integrations.domain import (
    IntegrationStatus,
    ResolvedIntegration,
    ResolvedRunBinding,
    ResourceBindingLevel,
    StoredResourceBinding,
)
from projectmind.integrations.repository import IntegrationRepository
from projectmind.runs.creation_participation import RunCreationAuthority
from projectmind.runs.creation_request import TaskRunIntent
from projectmind.runs.domain import (
    CreatedRun,
    CreateRunCommand,
    IdempotencyConflictError,
    RunStatus,
    TaskSourceSelectionError,
)
from projectmind.runs.repository import RunRepository
from projectmind.runs.service import RunService
from tests.runs.creation_fakes import creation_command, creation_intent
from tests.runs.test_task_run_service import _authorized_database, _resolved

_BROWSER_LOCK_EVENTS = [
    "lock:organizations",
    "lock:users",
    "lock:auth_sessions",
    "lock:projects",
    "lock:project_members",
]


class BoundaryFailure(RuntimeError):
    """実接続を持たずに、認領拒否や保存結果不明を注入する。"""


class _Transaction:
    """一つの session が所有する staged write と終了順を観測する。"""

    def __init__(self, owner: _Harness) -> None:
        """同じ session と transaction の検証状態を共有する。"""

        self.owner = owner

    async def __aenter__(self) -> Self:
        """二重 transaction を拒否し、以後の callback に同じ有効境界を要求する。"""

        assert not self.owner.active
        self.owner.active = True
        self.owner.events.append("begin")
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """本体失敗は回滚し、commit 応答喪失は保存有無に関係なく例外を伝播する。"""

        del error_type, traceback
        owner = self.owner
        try:
            if error is not None:
                owner.events.append("rollback")
            elif owner.commit_unknown is not None:
                owner.events.append("commit_unknown")
                if owner.commit_unknown:
                    owner.committed.extend(owner.staged)
                raise BoundaryFailure("commit result unknown")
            else:
                owner.events.append("commit")
                owner.committed.extend(owner.staged)
        finally:
            owner.staged.clear()
            owner.active = False


class _Session:
    """DB 接続を行わず、一つの use case 内の session identity を固定する。"""

    def __init__(self, owner: _Harness) -> None:
        """所有者だけが transaction を開閉できる seam を作る。"""

        self.owner = owner

    async def __aenter__(self) -> Self:
        """session の追加生成や途中切替を event 順序から検出できるようにする。"""

        self.owner.events.append("session_enter")
        return self

    async def __aexit__(self, *args: object) -> None:
        """transaction 完了後だけ session を閉じる。"""

        del args
        assert not self.owner.active
        self.owner.events.append("session_exit")

    def begin(self) -> _Transaction:
        """同じ owner の transaction を一つ返す。"""

        return _Transaction(self.owner)

    async def scalar(self, statement: Select[Any]) -> Any:
        """普通入口の実認証 SQL を、既存の明示条件付き fake へ渡す。"""

        self.owner.require_session(self)
        return await self.owner.database.scalar(statement)

    async def scalars(self, statement: Select[Any]) -> Any:
        """User と原 Session の条件・lock を省略せず同じ transaction で評価する。"""

        self.owner.require_session(self)
        return await self.owner.database.scalars(statement)

    async def flush(self) -> None:
        """普通入口だけの最終 flush を記録し、Worker への意図しない追加も検出する。"""

        self.owner.step("flush")


class _Participant:
    """新しい hash/Run を作らず、元要求・現権限・結算の callback だけを提供する。"""

    def __init__(self, owner: _Harness) -> None:
        """呼出し順と保存の観測先を保持する。"""

        self.owner = owner

    async def authorize(
        self,
        session: AsyncSession,
        *,
        intent: TaskRunIntent,
        idempotency_key: str,
    ) -> RunCreationAuthority:
        """caller の旧 role を使わず、同じ原 intent/key に対する現在値を返す。"""

        self.owner.require_session(session)
        assert intent.to_json() == self.owner.intent.to_json()
        assert idempotency_key == self.owner.key
        self.owner.step("authorize")
        return self.owner.authority

    async def before_create(self, session: AsyncSession) -> None:
        """元 Run が無い経路だけで、新しい作成の追加 gate を観測する。"""

        self.owner.require_session(session)
        self.owner.step("before_create")

    async def complete(self, session: AsyncSession, created: CreatedRun) -> None:
        """初期 snapshot と同じ transaction へ結算を仮保存し、失敗時の巻戻しを試す。"""

        self.owner.require_session(session)
        assert created.run_id == self.owner.created.run_id
        self.owner.staged.append("occurrence")
        self.owner.step("complete")


class _Harness:
    """本物の RunService に repository と transaction の局部 seam を注入する。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: str = "new") -> None:
        """原要求・解決済み task・一つの session と選択した return 経路を固定する。"""

        self.events: list[str] = []
        self.staged: list[str] = []
        self.committed: list[str] = []
        self.active = False
        self.failure: str | None = None
        self.commit_unknown: bool | None = None
        self.with_participant = True
        self.lookup_only = False
        self.path = path
        self.resolved = _resolved()
        self.intent = replace(
            creation_intent(),
            skill_version_id=self.resolved.skill_version_id,
            task_key=self.resolved.task_key,
        )
        self.database = _authorized_database(self.intent.project_id, self.intent.actor_id)
        self.database.on_lock = lambda entity: self.step(f"lock:{entity.__tablename__}")
        self.key = "original-scheduled-request"
        command = creation_command(self.intent)
        self.created = CreatedRun(
            run_id=uuid4(),
            project_id=command.project_id,
            task_id=command.task_id,
            status=RunStatus.QUEUED,
            row_version=1,
            created_at=datetime(2026, 9, 9, tzinfo=UTC),
            idempotent_replay=False,
        )
        self.replay = replace(self.created, row_version=7, idempotent_replay=True)
        self.lookups: list[CreatedRun | None] = (
            [self.replay]
            if path == "first_replay"
            else [None, self.replay]
            if path == "resource_winner"
            else [None, None]
        )
        self.authority = RunCreationAuthority("USER", "ACTIVE")
        self.command: CreateRunCommand | None = None
        self.replaced_sources: dict[str, Any] | None = None
        self.session = _Session(self)
        self.participant = _Participant(self)
        factory = cast(async_sessionmaker[AsyncSession], lambda: self.session)
        self.service = RunService(factory)
        integration = ResolvedIntegration(
            integration_id=uuid4(),
            project_id=self.intent.project_id,
            name="Synthetic source",
            kind="repository",
            provider="git",
            status=IntegrationStatus.ACTIVE,
            revision=1,
            capabilities=("repository.read/v1",),
            scope={},
            config={},
            secret_reference_id=None,
        )
        self.binding = ResolvedRunBinding(
            requirement_key="repository-source",
            resource_kind="repository",
            integration=integration,
            capability_version="repository.read/v1",
            scope={},
            source_binding_id=None,
        )
        self.frozen = StoredResourceBinding(
            binding_id=uuid4(),
            project_id=self.intent.project_id,
            scope_level=ResourceBindingLevel.RUN,
            scope_key=str(self.created.run_id),
            requirement_key=self.binding.requirement_key,
            resource_kind="repository",
            integration_id=integration.integration_id,
            run_id=self.created.run_id,
            source_binding_id=None,
            provider="git",
            capability_version="repository.read/v1",
            revision="1",
            scope={},
            checksum="a" * 64,
            created_by=self.intent.actor_id,
            created_at=self.created.created_at,
            updated_at=self.created.created_at,
            disabled_at=None,
        )

        async def lookup(
            repository: RunRepository,
            *,
            intent: TaskRunIntent,
            idempotency_key: str,
        ) -> CreatedRun | None:
            """既存の照会 seam へ渡った原要求と、授権が先行した事実を検証する。"""

            self.require_session(repository._session)
            assert intent.to_json() == self.intent.to_json() and idempotency_key == self.key
            if self.with_participant:
                assert "authorize" in self.events
            else:
                assert self.events[-len(_BROWSER_LOCK_EVENTS) :] == _BROWSER_LOCK_EVENTS
            self.step("lookup")
            return self.lookups.pop(0)

        async def resolve(
            *args: object, **kwargs: object
        ) -> tuple[dict[str, Any], tuple[ResolvedRunBinding, ...]]:
            """資源解決の成功・失効だけを注入し、resolver の業務規則を複製しない。"""

            del args
            repository = cast(IntegrationRepository, kwargs["integration_repository"])
            self.require_session(repository._session)
            self.events.append("resolve")
            if self.path == "resource_winner" or self.failure == "resolve":
                raise TaskSourceSelectionError("synthetic source no longer available")
            return {self.binding.requirement_key: {"provider": "git"}}, (self.binding,)

        async def create(repository: RunRepository, command: CreateRunCommand) -> CreatedRun:
            """新規 INSERT または既存 repository の唯一競争勝者を返す。"""

            self.require_session(repository._session)
            self.command = command
            if self.path != "unique_winner":
                self.staged.append("run")
            self.step("create")
            return self.replay if self.path == "unique_winner" else self.created

        async def freeze(
            repository: IntegrationRepository, **kwargs: object
        ) -> StoredResourceBinding:
            """初期 binding を結算より前に、同じ Run/actor へ固定することを観測する。"""

            self.require_session(repository._session)
            assert kwargs == {
                "run_id": self.created.run_id,
                "project_id": self.intent.project_id,
                "actor_id": self.intent.actor_id,
                "binding": self.binding,
            }
            self.staged.append("binding")
            self.step("freeze")
            return self.frozen

        async def replace_sources(
            repository: RunRepository,
            *,
            run_id: UUID,
            selected_sources: dict[str, Any],
        ) -> None:
            """公開 dispatch の commit 前に binding identity を初期 snapshot へ反映する。"""

            self.require_session(repository._session)
            assert run_id == self.created.run_id
            self.replaced_sources = deepcopy(selected_sources)
            self.staged.append("sources")
            self.step("replace_sources")

        monkeypatch.setattr(RunRepository, "find_task_run_replay", lookup)
        monkeypatch.setattr(RunRepository, "create_idempotent", create)
        monkeypatch.setattr(IntegrationRepository, "freeze_run_binding", freeze)
        monkeypatch.setattr(RunRepository, "replace_initial_selected_sources", replace_sources)
        monkeypatch.setattr("projectmind.runs.service._resolve_selected_sources", resolve)

    def require_session(self, session: object, *, transaction: bool = True) -> None:
        """callback が別 session/終了済み transaction に逃げていないことを検査する。"""

        assert session is self.session
        if transaction:
            assert self.active

    def step(self, name: str, *, transaction: bool = True) -> None:
        """境界を観測後に失敗させ、呼出し途中の仮保存も全体 rollback されるか調べる。"""

        if transaction:
            assert self.active
        self.events.append(name)
        if self.failure == name:
            raise BoundaryFailure(name)

    async def call(self) -> CreatedRun | None:
        """同じ原要求を、通常作成または照会専用の実 use case へ渡す。"""

        authorization = self.participant if self.with_participant else self.database.access
        if self.lookup_only:
            return await self.service.find_task_run_replay(
                project_id=self.intent.project_id,
                skill_version_id=self.intent.skill_version_id,
                task_key=self.intent.task_key,
                input_json=self.intent.input_json,
                sources=self.intent.sources,
                actor_id=self.intent.actor_id,
                idempotency_key=self.key,
                authorization=authorization,
            )
        return await self.service.create_task_run(
            project_id=self.intent.project_id,
            resolved=self.resolved,
            input_json=self.intent.input_json,
            sources=self.intent.sources,
            actor_id=self.intent.actor_id,
            idempotency_key=self.key,
            trace_id=None,
            authorization=authorization,
        )


@pytest.mark.parametrize(
    ("path", "steps"),
    [
        ("first_replay", ["authorize", "lookup", "complete"]),
        (
            "resource_winner",
            ["authorize", "lookup", "before_create", "resolve", "lookup", "complete"],
        ),
        (
            "unique_winner",
            ["authorize", "lookup", "before_create", "resolve", "create", "complete"],
        ),
        (
            "new",
            [
                "authorize",
                "lookup",
                "before_create",
                "resolve",
                "create",
                "freeze",
                "replace_sources",
                "complete",
            ],
        ),
    ],
)
async def test_all_creation_return_paths_complete_inside_the_original_transaction(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    steps: list[str],
) -> None:
    """初回照会・資源失効勝者・唯一勝者・新規作成の全 return が結算を commit 前に行う。"""

    harness = _Harness(monkeypatch, path)
    result = await harness.call()
    assert result == (harness.created if path == "new" else harness.replay)
    assert harness.events == ["session_enter", "begin", *steps, "commit", "session_exit"]
    assert harness.committed == (
        ["run", "binding", "sources", "occurrence"] if path == "new" else ["occurrence"]
    )
    assert not harness.active and not harness.staged


async def test_current_participant_authority_supplies_role_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唯一の授権元が返した USER/ACTIVE を snapshot に使い、caller role を受け取らない。"""

    harness = _Harness(monkeypatch)
    await harness.call()
    assert harness.command is not None
    permission = harness.command.permission_snapshot_json
    assert (
        permission["actor_system_role"] == "USER" and permission["project_membership"] == "ACTIVE"
    )
    assert permission["actor_id"] == str(harness.intent.actor_id)
    assert harness.replaced_sources == {
        harness.binding.requirement_key: {
            "provider": "git",
            "binding_id": str(harness.frozen.binding_id),
            "binding_checksum": harness.frozen.checksum,
            "binding_capability": harness.frozen.capability_version,
        }
    }


@pytest.mark.parametrize("found", [False, True])
async def test_lookup_only_authorizes_before_read_and_completes_only_an_existing_run(
    monkeypatch: pytest.MonkeyPatch,
    found: bool,
) -> None:
    """原 Run 照会も transaction 内で認証し、miss は新規作成や結算を開始しない。"""

    harness = _Harness(monkeypatch, "first_replay" if found else "new")
    harness.lookup_only = True
    assert await harness.call() == (harness.replay if found else None)
    assert harness.events == [
        "session_enter",
        "begin",
        "authorize",
        "lookup",
        *(["complete"] if found else []),
        "commit",
        "session_exit",
    ]
    assert harness.committed == (["occurrence"] if found else [])


@pytest.mark.parametrize("lookup_only", [False, True])
async def test_authority_refusal_happens_before_even_a_known_run_lookup(
    monkeypatch: pytest.MonkeyPatch,
    lookup_only: bool,
) -> None:
    """既存 Run が確実にあっても、現在の原認領授権を省略して情報を返さない。"""

    harness = _Harness(monkeypatch, "first_replay")
    harness.lookup_only = lookup_only
    harness.failure = "authorize"
    with pytest.raises(BoundaryFailure, match="authorize"):
        await harness.call()
    assert harness.events == ["session_enter", "begin", "authorize", "rollback", "session_exit"]
    assert not harness.committed and not harness.staged


@pytest.mark.parametrize(
    "failure", ["before_create", "create", "freeze", "replace_sources", "complete"]
)
async def test_participant_or_initial_snapshot_failure_rolls_back_the_entire_creation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """認領拒否や結算失敗で Run/binding だけが成功したように返さない。"""

    harness = _Harness(monkeypatch)
    harness.failure = failure
    with pytest.raises(BoundaryFailure, match=failure):
        await harness.call()
    assert harness.events[-3:] == [failure, "rollback", "session_exit"]
    assert not harness.committed and not harness.staged and not harness.active
    if failure == "before_create":
        assert "resolve" not in harness.events and "create" not in harness.events


@pytest.mark.parametrize("path", ["first_replay", "resource_winner", "unique_winner"])
async def test_replay_completion_failure_does_not_return_an_unsettled_success(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """元 Run があっても内部結算失敗を成功応答で隠さず、同じ認領を残す。"""

    harness = _Harness(monkeypatch, path)
    harness.failure = "complete"
    with pytest.raises(BoundaryFailure, match="complete"):
        await harness.call()
    assert harness.events[-3:] == ["complete", "rollback", "session_exit"]
    assert not harness.committed and not harness.staged


async def test_resource_failure_without_winner_preserves_original_error_and_no_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """資源失効後の元鍵再確認も miss なら、新 Run や結算を補造しない。"""

    harness = _Harness(monkeypatch)
    harness.failure = "resolve"
    with pytest.raises(TaskSourceSelectionError, match="synthetic source"):
        await harness.call()
    assert harness.events == [
        "session_enter",
        "begin",
        "authorize",
        "lookup",
        "before_create",
        "resolve",
        "lookup",
        "rollback",
        "session_exit",
    ]
    assert not harness.committed and not harness.staged


@pytest.mark.parametrize(
    "path", ["first_replay", "resource_winner", "unique_winner", "new", "lookup"]
)
@pytest.mark.parametrize("actually_committed", [False, True])
async def test_commit_unknown_never_returns_success_or_automatically_replays(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    actually_committed: bool,
) -> None:
    """return 値を準備していても __aexit__ の commit 失敗を伝え、保存有無を決め付けない。"""

    harness = _Harness(monkeypatch, "first_replay" if path == "lookup" else path)
    harness.lookup_only = path == "lookup"
    harness.commit_unknown = actually_committed
    with pytest.raises(BoundaryFailure, match="commit result unknown"):
        await harness.call()
    assert harness.events[-3:] == ["complete", "commit_unknown", "session_exit"]
    assert harness.events.count("begin") == harness.events.count("complete") == 1
    assert bool(harness.committed) is actually_committed
    assert not harness.staged and not harness.active


@pytest.mark.parametrize("lookup_only", [False, True])
async def test_ordinary_callers_use_original_session_without_worker_participation(
    monkeypatch: pytest.MonkeyPatch,
    lookup_only: bool,
) -> None:
    """通常入口も原会話 transaction で認証・flush し、Worker 専用結算は要求しない。"""

    harness = _Harness(monkeypatch, "first_replay" if lookup_only else "new")
    harness.lookup_only = lookup_only
    harness.with_participant = False
    assert await harness.call() == (harness.replay if lookup_only else harness.created)
    assert not {"authorize", "before_create", "complete"}.intersection(harness.events)
    assert harness.events[:7] == ["session_enter", "begin", *_BROWSER_LOCK_EVENTS]
    assert harness.events[-3:] == ["flush", "commit", "session_exit"]
    if lookup_only:
        assert harness.events == [
            "session_enter",
            "begin",
            *_BROWSER_LOCK_EVENTS,
            "lookup",
            "flush",
            "commit",
            "session_exit",
        ]
        assert not harness.committed
    else:
        assert harness.command is not None
        assert harness.command.permission_snapshot_json["actor_system_role"] == "USER"
        assert harness.command.permission_snapshot_json["project_membership"] == "ACTIVE"
        assert harness.committed == ["run", "binding", "sources"]


@pytest.mark.parametrize("lookup_only", [False, True])
@pytest.mark.parametrize("key", ["schedule:", "schedule:synthetic-original-occurrence"])
async def test_reserved_schedule_key_miss_refuses_ordinary_lookup_and_creation(
    monkeypatch: pytest.MonkeyPatch,
    lookup_only: bool,
    key: str,
) -> None:
    """同じ key 領域を通常入口から新規占有して、認領・重複・残枠制御を迂回させない。"""

    harness = _Harness(monkeypatch)
    harness.lookup_only = lookup_only
    harness.with_participant = False
    harness.key = key
    with pytest.raises(IdempotencyConflictError):
        await harness.call()
    expected = [
        "session_enter",
        "begin",
        *_BROWSER_LOCK_EVENTS,
        "lookup",
        "rollback",
        "session_exit",
    ]
    assert harness.events == expected
    assert not harness.committed and not harness.staged
    assert harness.command is None


@pytest.mark.parametrize("lookup_only", [False, True])
@pytest.mark.parametrize("key", ["schedule:", "schedule:synthetic-original-occurrence"])
async def test_existing_original_reserved_key_can_still_be_confirmed_without_participant(
    monkeypatch: pytest.MonkeyPatch,
    lookup_only: bool,
    key: str,
) -> None:
    """元要求の照合が先行し、既存の予約名 key を新しい禁止規則で読めなくしない。"""

    harness = _Harness(monkeypatch, "first_replay")
    harness.lookup_only = lookup_only
    harness.with_participant = False
    harness.key = key
    assert await harness.call() == harness.replay
    expected = [
        "session_enter",
        "begin",
        *_BROWSER_LOCK_EVENTS,
        "lookup",
        "flush",
        "commit",
        "session_exit",
    ]
    assert harness.events == expected
    assert not harness.committed and harness.command is None


async def test_authorized_participant_can_create_with_original_reserved_schedule_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式な内部 participant は授権・認領確認・同時結算を経て元 key をそのまま使う。"""

    harness = _Harness(monkeypatch)
    harness.key = "schedule:synthetic-original-occurrence"
    assert await harness.call() == harness.created
    assert harness.command is not None and harness.command.idempotency_key == harness.key
    assert harness.events == [
        "session_enter",
        "begin",
        "authorize",
        "lookup",
        "before_create",
        "resolve",
        "create",
        "freeze",
        "replace_sources",
        "complete",
        "commit",
        "session_exit",
    ]
    assert harness.committed == ["run", "binding", "sources", "occurrence"]
