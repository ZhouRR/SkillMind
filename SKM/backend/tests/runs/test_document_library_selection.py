"""文書庫保存先を選択・凍結し、入力参照/履歴と未開放の実行権限を分離する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skillmind.documents.library import (
    DOCUMENT_LIBRARY_SELECTION,
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryBindingRepository,
    FrozenDocumentLibraryBinding,
    ResolvedDocumentLibraryBinding,
    parse_document_library_source,
)
from skillmind.documents.references import choice_document_ids, run_document_ids
from skillmind.integrations.repository import IntegrationRepository
from skillmind.runs.domain import TaskSourceSelectionError
from skillmind.runs.repository import RunRepository
from skillmind.runs.resource_projection import document_snapshots, source_summaries
from skillmind.runs.service import RunService, _resolve_selected_sources
from tests.documents.test_document_library_binding import target
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.runs.test_task_run_service import _authorized_database, _resolved

REQUIREMENT = {
    "key": "outputs",
    "kind": "document",
    "required": True,
    "access": "write",
    "capabilities": [DOCUMENT_WRITE_CAPABILITY],
}


async def resolve(*, token=DOCUMENT_LIBRARY_SELECTION, requirement=None, library=None):
    """同 Project の明示選択だけを本番 resolver へ渡し、Integration 照会を監視する。"""

    definition = REQUIREMENT if requirement is None else requirement
    project_id = uuid4()
    integrations = AsyncMock(spec=IntegrationRepository)
    result = await _resolve_selected_sources(
        _resolved((definition,)),
        {"outputs": token},
        blueprint={"resource_requirements": [definition]},
        project_id=project_id,
        integration_repository=integrations,
        document_library_target=library,
    )
    assert not integrations.mock_calls
    return project_id, result


async def test_library_selection_freezes_only_the_configured_project_target():
    """選択文字列から bucket/接続を受け取らず、入力文書の全集も読み取らない。"""

    library = target()
    project_id, (sources, bindings) = await resolve(library=library)
    assert bindings == (ResolvedDocumentLibraryBinding("outputs", library),)
    assert sources["outputs"]["scope"] == library.scope(project_id)
    assert "integration_id" not in sources["outputs"]
    assert "document_snapshot" not in sources["outputs"]


@pytest.mark.parametrize(
    "token,changes,configured",
    [
        ("project-library:other", {}, True),
        ("project-documents:all", {}, True),
        (f"document:{uuid4()}", {}, True),
        (f"integration:{uuid4()}", {}, True),
        (DOCUMENT_LIBRARY_SELECTION, {}, False),
        (DOCUMENT_LIBRARY_SELECTION, {"access": "read"}, True),
        (DOCUMENT_LIBRARY_SELECTION, {"capabilities": ["document.read/v1"]}, True),
        (
            DOCUMENT_LIBRARY_SELECTION,
            {"capabilities": [DOCUMENT_WRITE_CAPABILITY, "document.read/v1"]},
            True,
        ),
    ],
)
async def test_read_selection_or_unknown_destination_cannot_become_library_write(
    token,
    changes,
    configured,
):
    """候補 hint だけで読取資源の write 昇格や保存先指定を許可しない。"""

    with pytest.raises(TaskSourceSelectionError):
        await resolve(
            token=token,
            requirement={**REQUIREMENT, **changes},
            library=target() if configured else None,
        )


@pytest.mark.parametrize(
    "deferred,database", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_candidate_does_not_open_disabled_write_even_without_apply_intent(
    deferred, database
):
    """既存配備 switch と候補配置を組み合わせても、Run/Binding/Outbox を作成しない。"""

    db = _authorized_database(uuid4(), uuid4())
    service = RunService(
        db.session_factory,
        deferred_features_enabled=deferred,
        database_writes_enabled=database,
        document_library_target=target(),
    )
    with (
        patch.object(RunRepository, "find_task_run_replay", new=AsyncMock(return_value=None)),
        patch.object(RunRepository, "create_idempotent", new=AsyncMock()) as create,
        patch.object(DocumentLibraryBindingRepository, "freeze", new=AsyncMock()) as freeze,
        pytest.raises(TaskSourceSelectionError, match="not enabled"),
    ):
        await service.create_task_run(
            project_id=db.project.id,
            actor_id=db.user.id,
            resolved=_resolved((REQUIREMENT,)),
            input_json={},
            sources={"outputs": DOCUMENT_LIBRARY_SELECTION},
            idempotency_key="fixture-library",
            trace_id=None,
            authorization=db.access,
        )
    create.assert_not_awaited()
    freeze.assert_not_awaited()
    assert db.commits == 0 and db.rollbacks == 1


async def test_creation_uses_original_run_binding_before_commit_when_capability_is_available():
    """内部の文書 write 上限を明示し、本番作成/凍結を共有認証 transaction 内で通す。"""

    db = _authorized_database(uuid4(), uuid4())
    library = target()
    captured = {}
    stored_bindings = []

    async def create(self, command):
        """Run INSERT の代わりに永続化 port を捕捉する。SQL/FK の実証ではない。"""
        row = stored_creation(command)
        captured["run"] = row
        return self._to_created_run(row, idempotent_replay=False)

    async def save_sources(self, *, run_id, selected_sources):
        """commit 前にだけ元 Run へ完全 snapshot を反映する。"""
        assert db.commits == 0 and run_id == captured["run"].id
        captured["run"].selected_sources_json = deepcopy(selected_sources)

    original_freeze = DocumentLibraryBindingRepository.freeze

    async def freeze(self, **kwargs):
        """本物 repository に transaction port だけを差し替え、行と hash を検証する。"""
        assert db.commits == 0
        session = MagicMock()
        session.flush = AsyncMock()
        repository = DocumentLibraryBindingRepository(session, target=library)
        binding = await original_freeze(repository, **kwargs)
        stored_bindings.append(binding)
        return binding

    with (
        patch.object(RunRepository, "find_task_run_replay", new=AsyncMock(return_value=None)),
        patch.object(RunRepository, "create_idempotent", new=create),
        patch.object(RunRepository, "replace_initial_selected_sources", new=save_sources),
        patch.object(DocumentLibraryBindingRepository, "freeze", new=freeze),
        patch.object(IntegrationRepository, "freeze_run_binding", new=AsyncMock()) as integration,
    ):
        result = await RunService(
            db.session_factory, document_library_target=library,
            deferred_features_enabled=False, document_writes_enabled=True,
        ).create_task_run(
            project_id=db.project.id,
            actor_id=db.user.id,
            resolved=_resolved((REQUIREMENT,)),
            input_json={},
            sources={"outputs": DOCUMENT_LIBRARY_SELECTION},
            idempotency_key="fixture-library",
            trace_id=None,
            authorization=db.access,
        )
    integration.assert_not_awaited()
    assert db.commits == 1 and len(stored_bindings) == 1
    source = captured["run"].selected_sources_json["outputs"]
    snapshot = parse_document_library_source(
        source, project_id=db.project.id, run_id=result.run_id, requirement_key="outputs"
    )
    assert snapshot.binding_id == stored_bindings[0].id
    assert source["binding_checksum"] == stored_bindings[0].checksum
    assert run_document_ids(captured["run"]) == frozenset()


def frozen_run():
    """原作成意図と保存先 scope/hash が一致する歴史 Run を作る。"""

    intent = creation_intent(sources={"outputs": DOCUMENT_LIBRARY_SELECTION})
    row = stored_creation(creation_command(intent))
    row.selected_sources_json = {
        "outputs": FrozenDocumentLibraryBinding(
            row.project_id, row.id, uuid4(), "outputs", target()
        ).to_json()
    }
    return row


def test_library_has_no_input_members_and_projection_does_not_expose_storage_scope():
    """保存先を LEGACY_UNAVAILABLE 入力や空の全集にせず、公開摘要に内部 locator を出さない。"""

    row = frozen_run()
    assert choice_document_ids({"outputs": DOCUMENT_LIBRARY_SELECTION}) == frozenset()
    assert run_document_ids(row) == frozenset()
    assert document_snapshots(row.selected_sources_json, project_id=row.project_id) == ()
    assert source_summaries(row.selected_sources_json) == {
        "outputs": {
            "provider": "project-library",
            "capability": DOCUMENT_WRITE_CAPABILITY,
            "resource_kind": "document",
            "access": "write",
        }
    }


@pytest.mark.parametrize(
    "change", ["extra_snapshot", "prefix", "hash", "slot", "binding", "project"]
)
def test_library_marker_cannot_hide_invalid_or_replaced_input_references(change):
    """保存先を名乗るだけでは削除保護から除外せず、元 snapshot の破損を公開 INVALID にする。"""

    row = frozen_run()
    source = row.selected_sources_json["outputs"]
    if change == "extra_snapshot":
        source["document_snapshot"] = {}
    elif change == "prefix":
        source["scope"]["key_prefix"] = "other-project/"
    elif change == "hash":
        source["binding_checksum"] = "sha256:" + "0" * 64
    elif change == "slot":
        row.selected_sources_json = {"different": source}
    elif change == "binding":
        source["binding_id"] = str(uuid4())
    else:
        source["scope"]["project_id"] = str(uuid4())
    with pytest.raises(ValueError):
        run_document_ids(row)
    result = document_snapshots(row.selected_sources_json, project_id=row.project_id)
    assert len(result) == 1 and result[0].status == "INVALID"


def test_snapshot_from_another_run_or_missing_original_library_is_not_reference_free():
    """Project/slot が同じでも他 Run snapshot や消失した選択を参照不要と解釈しない。"""

    row = frozen_run()
    source = parse_document_library_source(
        row.selected_sources_json["outputs"], project_id=row.project_id, requirement_key="outputs"
    )
    row.selected_sources_json["outputs"] = replace(source, run_id=uuid4()).to_json()
    with pytest.raises(ValueError):
        run_document_ids(row)
    row.selected_sources_json.clear()
    with pytest.raises(ValueError):
        run_document_ids(row)


@pytest.mark.parametrize(
    "capability,provider", [("database.read/v1", "postgres"), ("mcp.read/v1", "mcp")]
)
def test_library_can_coexist_with_existing_non_document_read_sources(capability, provider):
    """PG/MCP の追加で無関係な文書の参照保護を一律不能にせず、入力清単は別途検証する。"""

    token = f"integration:{uuid4()}"
    intent = creation_intent(sources={"outputs": DOCUMENT_LIBRARY_SELECTION, "records": token})
    row = stored_creation(creation_command(intent))
    row.selected_sources_json = {
        "outputs": FrozenDocumentLibraryBinding(
            row.project_id, row.id, uuid4(), "outputs", target()
        ).to_json(),
        "records": {"capability": capability, "provider": provider, "candidate_key": token},
    }
    assert run_document_ids(row) == frozenset()
    row.selected_sources_json["records"]["document_snapshot"] = {}
    with pytest.raises(ValueError):
        run_document_ids(row)
