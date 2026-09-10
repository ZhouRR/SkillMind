"""通用 task Run 作成 service の source 選択と snapshot 構築を検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Self
from unittest.mock import AsyncMock, Mock, patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from skillmind.documents.domain import StoredDocument
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.snapshot import ALL_DOCUMENTS_SELECTION, DOCUMENT_READ_CAPABILITY
from skillmind.integrations.domain import IntegrationStatus, ResolvedIntegration
from skillmind.integrations.repository import IntegrationRepository
from skillmind.runs.creation_request import TaskRunIntent
from skillmind.runs.domain import (
    CreatedRun,
    CreateRunCommand,
    RunStatus,
    TaskSourceSelectionError,
)
from skillmind.runs.repository import RunRepository
from skillmind.runs.service import M0_DENIED_BUILTIN_TOOLS, RunService, _resolve_selected_sources
from skillmind.skills import ResolvedTaskRun
from tests.documents.fakes import document_content, stored_document
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.schedules.authorization_harness import ScheduleAuthorizationDatabase


def _authorized_database(
    project_id: UUID, actor_id: UUID, *, admin: bool = False
) -> ScheduleAuthorizationDatabase:
    """既存 SQL fake の原会話と所属を対象へ揃え、共通認証 repository を実際に通す。"""

    database = ScheduleAuthorizationDatabase()
    role = "ADMIN" if admin else "USER"
    database.user.id = actor_id
    database.user.system_role = role
    database.auth_session.user_id = actor_id
    database.auth_session.system_role_at_login = role
    database.access = replace(
        database.access,
        actor=replace(database.access.actor, user_id=actor_id, system_role=role),
    )
    database.project.id = project_id
    assert database.member is not None
    database.member.project_id = project_id
    database.member.user_id = actor_id
    return database


def _resolved(
    requirements: tuple[dict[str, object], ...] = (),
) -> ResolvedTaskRun:
    """Run 作成テスト用の解決済み task を組み立てる。

    資源要求の宣言元は blueprint 一本のため、frozen manifest の
    `capability_blueprint.resource_requirements` へ直接載せる。
    """

    return ResolvedTaskRun(
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="repository-review",
        version="1.0.0",
        task_key="review-change",
        capability="repository.review",
        task_type="immediate",
        manifest_checksum="sha256:" + ("a" * 64),
        allowed_capabilities=("repository.read/v1",),
        skill_snapshot={
            "skill_version_id": "sv",
            "manifest_checksum": "sha256:x",
            "sort_order": 0,
            "manifest": {
                "capability_blueprint": {
                    "execution_preferences": {"recommended_profile": "SUPERVISED"},
                    "resource_requirements": list(requirements),
                }
            },
        },
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": False},
        input_schema_checksum="sha256:" + ("b" * 64),
        output_schema_checksum="sha256:" + ("c" * 64),
        task_output_schema=None,
        task_output_schema_checksum=None,
    )


_REPOSITORY_SOURCE: dict[str, object] = {
    "key": "repository-source",
    "kind": "repository",
    "required": True,
    "access": "read",
    "capabilities": ["repository.read/v1"],
    "accepted_providers": ["git"],
}
_OPTIONAL_ISSUE_SOURCE: dict[str, object] = {
    "key": "issue-source",
    "kind": "issue",
    "required": False,
    "access": "read",
    "capabilities": ["issue.read/v1"],
    "accepted_providers": ["csv", "redmine"],
}


class _NoConfiguredBindings:
    """明示選択の拒否 test で持続 binding を返さない repository。"""

    async def find_configured_binding(self, **kwargs: object) -> None:
        """Project/Task binding が無い状態を返す。"""

        del kwargs
        return None


async def _no_configured_binding(self: IntegrationRepository, **kwargs: object) -> None:
    """未設定の Project/Task ResourceBinding を再現する。"""

    del self, kwargs
    return None


async def _resolve_without_bindings(
    resolved: ResolvedTaskRun, sources: dict[str, str]
) -> dict[str, object]:
    """持続 binding の無い選択を本番 resolver で検証する。"""

    blueprint = resolved.skill_snapshot["manifest"]["capability_blueprint"]
    selected, bindings = await _resolve_selected_sources(
        resolved,
        sources,
        blueprint=blueprint,
        project_id=uuid4(),
        integration_repository=_NoConfiguredBindings(),  # type: ignore[arg-type]
    )
    assert bindings == ()
    return selected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["git", "svn", "csv", "redmine"])
async def test_resolve_selected_sources_rejects_bare_provider_names(provider: str) -> None:
    """受理 hint に一致しても Provider 名だけでは実資源を特定できない。"""

    requirement = {**_REPOSITORY_SOURCE, "accepted_providers": [provider]}
    with pytest.raises(TaskSourceSelectionError, match="must identify an Integration"):
        await _resolve_without_bindings(
            _resolved((requirement,)), {"repository-source": provider}
        )


@pytest.mark.asyncio
async def test_resolve_selected_sources_omits_absent_optional_source() -> None:
    """任意 source は未選択なら snapshot から除外する。"""

    selected = await _resolve_without_bindings(_resolved((_OPTIONAL_ISSUE_SOURCE,)), {})

    assert selected == {}


@pytest.mark.asyncio
async def test_resolve_selected_sources_rejects_missing_required_source() -> None:
    """必須 source の未選択は拒否する。"""

    with pytest.raises(TaskSourceSelectionError):
        await _resolve_without_bindings(_resolved((_REPOSITORY_SOURCE,)), {})


@pytest.mark.asyncio
async def test_resolve_selected_sources_rejects_unaccepted_provider() -> None:
    """Manifest が受理しない provider を拒否する。"""

    with pytest.raises(TaskSourceSelectionError):
        await _resolve_without_bindings(
            _resolved((_REPOSITORY_SOURCE,)), {"repository-source": "svn"}
        )


@pytest.mark.asyncio
async def test_resolve_selected_sources_rejects_unknown_source_key() -> None:
    """Manifest に無い data source key を拒否する。"""

    with pytest.raises(TaskSourceSelectionError):
        await _resolve_without_bindings(_resolved(), {"ghost": "x"})


class _Transaction:
    """テスト用の async transaction context。"""

    async def __aenter__(self) -> Self:
        """何も開かずに自身を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """後始末は不要。"""

        return None


class _Session:
    """Worker tick が使う最小 session seam。普通作成の認証には使用しない。"""

    async def __aenter__(self) -> Self:
        """何も接続せずに自身を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """後始末は不要。"""

        return None

    def begin(self) -> _Transaction:
        """テスト用 transaction context を返す。"""

        return _Transaction()


@pytest.mark.asyncio
async def test_create_task_run_freezes_generic_snapshot() -> None:
    """通用 Run が精確 version と Manifest 由来の権限・source を snapshot へ固定する。"""

    resolved = _resolved((_REPOSITORY_SOURCE,))
    captured: dict[str, CreateRunCommand] = {}

    async def _fake_create(self: RunRepository, command: CreateRunCommand) -> CreatedRun:
        """渡された command を捕捉して固定 Run を返す。"""

        captured["command"] = command
        return CreatedRun(
            run_id=uuid4(),
            project_id=command.project_id,
            task_id=command.task_id,
            status=RunStatus.QUEUED,
            row_version=1,
            created_at=datetime(2026, 7, 9, tzinfo=UTC),
            idempotent_replay=False,
        )

    project_id = uuid4()
    actor_id = uuid4()
    database = _authorized_database(project_id, actor_id, admin=True)
    integration = ResolvedIntegration(
        integration_id=uuid4(),
        project_id=project_id,
        name="Synthetic repository",
        kind="repository",
        provider="git",
        status=IntegrationStatus.ACTIVE,
        revision=1,
        capabilities=("repository.read/v1",),
        scope={"paths": ["src/"]},
        config={},
        secret_reference_id=None,
    )
    candidate = f"integration:{integration.integration_id}"
    frozen = Mock(binding_id=uuid4(), checksum="a" * 64, capability_version="repository.read/v1")
    with (
        patch.object(RunRepository, "find_task_run_replay", new=AsyncMock(return_value=None)),
        patch.object(RunRepository, "create_idempotent", new=_fake_create),
        patch.object(
            IntegrationRepository,
            "find_configured_binding",
            new=_no_configured_binding,
        ),
        patch.object(
            IntegrationRepository, "get_integration", new=AsyncMock(return_value=integration)
        ),
        patch.object(
            IntegrationRepository, "freeze_run_binding", new=AsyncMock(return_value=frozen)
        ) as freeze,
        patch.object(
            RunRepository, "replace_initial_selected_sources", new=AsyncMock()
        ) as replace_sources,
    ):
        run = await RunService(database.session_factory).create_task_run(
            project_id=project_id,
            resolved=resolved,
            input_json={"target": "main"},
            sources={"repository-source": candidate},
            idempotency_key="req-1",
            trace_id="trace-1",
            actor_id=actor_id,
            authorization=database.access,
        )

    assert run.status is RunStatus.QUEUED
    command = captured["command"]
    expected_task_id = uuid5(
        NAMESPACE_URL, f"skillmind:task:{resolved.skill_version_id}:{resolved.task_key}"
    )
    assert command.task_id == expected_task_id
    assert command.task_snapshot_json["task_key"] == "review-change"
    assert command.task_snapshot_json["capability"] == "repository.review"
    assert command.task_snapshot_json["skill_version_id"] == str(resolved.skill_version_id)
    assert command.task_snapshot_json["input_schema"] == resolved.input_schema_checksum
    assert command.task_snapshot_json["input_schema_json"] == resolved.input_schema
    assert command.task_snapshot_json["output_schema_checksum"] == resolved.output_schema_checksum
    assert command.task_snapshot_json["result_kind"] == "OUTCOME_ENVELOPE"
    assert command.task_snapshot_json["task_output_schema_json"] is None
    assert command.task_snapshot_json["skill_snapshots"] == [resolved.skill_snapshot]
    # 扇出は実行時判断のため無条件に付く (計画 §23 D2)。子は Run 予算を分け合うだけで
    # 上限を増やさないので、付与そのものが費用や権限の拡大にはならない。
    assert command.permission_snapshot_json["allowed_capabilities"] == [
        "interaction.request/v1",
        "repository.read/v1",
        "subagent.dispatch/v1",
    ]
    assert command.permission_snapshot_json["denied_builtin_tools"] == list(M0_DENIED_BUILTIN_TOOLS)
    assert command.permission_snapshot_json["actor_id"] == str(actor_id)
    assert command.permission_snapshot_json["actor_system_role"] == "ADMIN"
    assert command.permission_snapshot_json["project_membership"] == "ADMIN_BYPASS"
    assert command.permission_snapshot_json["execution_profile"] == "SUPERVISED"
    assert command.selected_sources_json == {
        "repository-source": {
            "capability": "repository.read/v1",
            "provider": "git",
            "candidate_key": candidate,
            "integration_id": str(integration.integration_id),
            "revision": "1",
            "scope": {"paths": ["src/"]},
            "resource_kind": "repository",
            "access": "read",
            "source_binding_id": None,
        }
    }
    freeze.assert_awaited_once()
    replace_sources.assert_awaited_once_with(
        run_id=run.run_id,
        selected_sources={
            "repository-source": {
                **command.selected_sources_json["repository-source"],
                "binding_id": str(frozen.binding_id),
                "binding_checksum": frozen.checksum,
                "binding_capability": frozen.capability_version,
            }
        },
    )
    assert command.limits_snapshot_json["wall_timeout_seconds"] == 900
    assert command.skill_snapshots_json == (resolved.skill_snapshot,)
    assert database.transactions == database.commits == 1
    database.session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_task_run_rejects_invalid_source_before_persistence() -> None:
    """Source 選択が不正なら永続化前に拒否する。"""

    database = _authorized_database(uuid4(), uuid4())
    with (
        patch.object(RunRepository, "find_task_run_replay", new=AsyncMock(return_value=None)),
        patch.object(RunRepository, "create_idempotent") as create,
        patch.object(
            IntegrationRepository,
            "find_configured_binding",
            new=_no_configured_binding,
        ),
        pytest.raises(TaskSourceSelectionError),
    ):
        await RunService(database.session_factory).create_task_run(
            project_id=database.project.id,
            resolved=_resolved((_REPOSITORY_SOURCE,)),
            input_json={},
            sources={},
            idempotency_key="req-2",
            trace_id=None,
            actor_id=database.user.id,
            authorization=database.access,
        )
    create.assert_not_called()
    assert database.rollbacks == 1 and database.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ALL", "SINGLE", "SET"])
async def test_document_create_replay_keeps_original_members_without_reading_live_sources(
    mode: str,
) -> None:
    """初回は実 resolver で凍結し、変更/削除後の再送では現在の文書に到達しない。"""

    resolved = _resolved(
        (
            {
                "key": "docs",
                "kind": "document",
                "required": True,
                "access": "read",
                "capabilities": [DOCUMENT_READ_CAPABILITY],
            },
        )
    )
    project_id, actor_id = uuid4(), uuid4()
    documents = [
        stored_document(project_id, document_content(name="first.md")),
        stored_document(project_id, document_content(name="second.md")),
    ]
    first_id, second_id = (item.document_id for item in documents)
    token = (
        ALL_DOCUMENTS_SELECTION
        if mode == "ALL"
        else f"document:{first_id}"
        if mode == "SINGLE"
        else f"documents:{second_id},{first_id}"
    )
    captured: list[CreateRunCommand] = []

    async def create(self: RunRepository, command: CreateRunCommand) -> CreatedRun:
        """実 resolver が生成した command を初回 commit の事実として保存する。"""

        captured.append(command)
        return self._to_created_run(stored_creation(command), idempotent_replay=False)

    async def get_document(
        self: DocumentRepository, *, project_id: object, document_id: object
    ) -> StoredDocument:
        """選ばれた ID の metadata だけを fixture から返す。"""

        del self
        return next(
            item
            for item in documents
            if item.project_id == project_id and item.document_id == document_id
        )

    database = _authorized_database(project_id, actor_id)
    service = RunService(database.session_factory)
    with (
        patch.object(RunRepository, "_load_by_idempotency", new=AsyncMock(return_value=None)),
        patch.object(RunRepository, "create_idempotent", new=create),
        patch.object(DocumentRepository, "list_for_project", new=AsyncMock(return_value=documents)),
        patch.object(DocumentRepository, "get", new=get_document),
    ):
        first = await service.create_task_run(
            project_id=project_id,
            resolved=resolved,
            input_json={},
            sources={"docs": token},
            actor_id=actor_id,
            authorization=database.access,
            idempotency_key="one-request",
            trace_id="first",
        )
    row = stored_creation(captured[0])
    row.id = first.run_id
    row.status = RunStatus.SUCCEEDED.value
    row.row_version = 7
    before = deepcopy(row.selected_sources_json)
    if mode == "SET":
        token = f"documents:{first_id},{second_id}"
    with (
        patch.object(RunRepository, "_load_by_idempotency", new=AsyncMock(return_value=row)),
        patch(
            "skillmind.runs.service._resolve_selected_sources",
            new=AsyncMock(side_effect=AssertionError("live resources must not be read")),
        ) as resolve,
        patch.object(RunRepository, "create_idempotent", new=AsyncMock()) as insert,
    ):
        replay = await service.create_task_run(
            project_id=project_id,
            resolved=resolved,
            input_json={},
            sources={"docs": token},
            actor_id=actor_id,
            authorization=database.access,
            idempotency_key="one-request",
            trace_id="retry",
        )
    assert replay.run_id == first.run_id
    assert replay.status is RunStatus.SUCCEEDED
    assert replay.row_version == 7
    assert replay.idempotent_replay is True
    assert row.selected_sources_json == before
    snapshot = before["docs"]["document_snapshot"]
    assert snapshot["selection_mode"] == mode
    assert len(snapshot["documents"]) == (1 if mode == "SINGLE" else 2)
    resolve.assert_not_awaited()
    insert.assert_not_awaited()
    assert database.transactions == database.commits == 2
    assert database.session.flush.await_count == 2


@pytest.mark.asyncio
async def test_resource_resolution_failure_rechecks_a_concurrent_committed_winner() -> None:
    """解析中の資源失効だけを見て、既に commit した同一要求を失敗扱いしない。"""

    resolved = _resolved()
    intent = replace(
        creation_intent(), skill_version_id=resolved.skill_version_id, task_key=resolved.task_key
    )
    row = stored_creation(creation_command(intent))
    database = _authorized_database(intent.project_id, intent.actor_id)
    with (
        patch.object(
            RunRepository, "_load_by_idempotency", new=AsyncMock(side_effect=[None, row])
        ) as lookup,
        patch(
            "skillmind.runs.service._resolve_selected_sources",
            new=AsyncMock(side_effect=TaskSourceSelectionError("Document was removed")),
        ),
        patch.object(RunRepository, "create_idempotent", new=AsyncMock()) as insert,
    ):
        replay = await RunService(database.session_factory).create_task_run(
            project_id=intent.project_id,
            resolved=resolved,
            input_json=intent.input_json,
            sources=intent.sources,
            actor_id=intent.actor_id,
            authorization=database.access,
            idempotency_key=row.idempotency_key,
            trace_id=None,
        )
    assert replay.run_id == row.id
    assert replay.idempotent_replay is True
    assert lookup.await_count == 2
    insert.assert_not_awaited()
    assert database.transactions == database.commits == 1
    database.session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_replay_entry_uses_the_same_repository_identity_check() -> None:
    """API/調度の事前照会と create 内照会で別の同一性規則を作らない。"""

    intent: TaskRunIntent = creation_intent()
    row = stored_creation(creation_command(intent))
    database = _authorized_database(intent.project_id, intent.actor_id)
    with patch.object(RunRepository, "_load_by_idempotency", new=AsyncMock(return_value=row)):
        replay = await RunService(database.session_factory).find_task_run_replay(
            project_id=intent.project_id,
            skill_version_id=intent.skill_version_id,
            task_key=intent.task_key,
            input_json=intent.input_json,
            sources=intent.sources,
            actor_id=intent.actor_id,
            authorization=database.access,
            idempotency_key=row.idempotency_key,
        )
    assert replay is not None and replay.run_id == row.id
    assert database.transactions == database.commits == 1
    database.session.flush.assert_awaited_once()
