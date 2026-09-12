"""文書の入力/保存先を実 ContextBuilder で解決し、提案以外の write 公開を拒否する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.context_builder import (
    ContractStore,
    ProductionRunContextBuilder,
    create_run_tool_registry,
)
from skillmind.agent.task_brief import _resources
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.core.hashing import canonical_json
from skillmind.documents.library import FrozenDocumentLibraryBinding
from skillmind.skills.task_catalog import _allowed_capabilities
from tests.agent.input_fakes import MemoryInputSnapshots
from tests.agent.test_runtime_context import (
    CONTRACTS,
    _document_manifest,
    _generic_claimed,
    _NoopDocumentSource,
)
from tests.agent.test_workspace_materializer import _FakeInventory
from tests.documents.fakes import document_content, document_snapshot
from tests.documents.test_document_library_binding import target


def library_run(*, with_input=False, requirement_changes=None, operation="CREATE"):
    """Skill 固有の内容を使わず、原 Run/Project に結び付いた保存 slot を作る。"""

    manifest = _document_manifest()
    capabilities = ["change.propose/v1"]
    if with_input:
        capabilities.append("document.read/v1")
    else:
        manifest["capability_blueprint"]["resource_requirements"] = []
    manifest["permissions"] = {"external_write_policy": "deny"}
    manifest["tools"] = [{"capability": cap, "required": True} for cap in capabilities]
    manifest["capability_blueprint"]["resource_requirements"].append(
        {
            "key": "outputs",
            "kind": "document",
            "required": True,
            "access": "write",
            "capabilities": ["document.write/v1"],
            **(requirement_changes or {}),
        }
    )
    manifest["capability_blueprint"]["effect_intents"] = [
        {
            "key": "save",
            "resource_key": "outputs",
            "mode": "apply",
            "operation": operation,
            "risk": "low",
        }
    ]
    # 実 catalog と同じく apply は Agent 権限へ含めず、保存 slot にだけ宣言する。
    claimed = _generic_claimed(
        manifest=manifest, allowed=_allowed_capabilities(manifest), selected_sources={}
    )
    library = target()
    claimed.selected_sources_json["outputs"] = FrozenDocumentLibraryBinding(
        claimed.project_id, claimed.run_id, uuid4(), "outputs", library
    ).to_json()
    return claimed, library


def builder(tmp_path, library, *, enabled=True, materializer=None, source=None):
    """実 registry と共有配備門禁を使い、外部ストレージ接続なしで準備する。"""

    return ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(tmp_path / "runs"),
        tool_registry=create_run_tool_registry(
            ContractStore(CONTRACTS),
            document_source=source if source is not None else _NoopDocumentSource(),
            deferred_features_enabled=False,
            document_writes_enabled=enabled,
        ),
        model="claude-test",
        materializer=materializer,
        deferred_features_enabled=False,
        document_writes_enabled=enabled,
        document_library_target=library,
    )


async def test_library_only_context_exposes_proposal_without_reading_documents(tmp_path: Path):
    """空文書庫でも保存先を解決し、読取/直接 write/子 Agent を登録しない。"""

    claimed, library = library_run()
    assert claimed.permission_snapshot_json["allowed_capabilities"] == ["change.propose/v1"]
    source = AsyncMock()
    inventory = _FakeInventory([document_content()])
    materializer = WorkspaceMaterializer(
        document_inventory=inventory,
        input_snapshots=MemoryInputSnapshots(claimed),
        max_bytes=10_485_760,
        max_files=500,
    )
    context = await builder(tmp_path, library, source=source, materializer=materializer).build(
        claimed, sequence_start=1
    )
    assert [tool.capability for tool in context.tools] == ["change.propose/v1"]
    assert not source.mock_calls
    assert inventory.calls == 0
    resource = context.task_brief["resources"][0]
    assert resource["binding"] == {
        "capability": "document.write/v1",
        "provider": "project-library",
        "binding_source": "RUN_PREFLIGHT",
    }
    assert "materialization" not in resource
    assert context.task_brief["effect_policy"]["declared_intents"][0]["executable"] is False
    assert context.task_brief["identity"]["project_id"] == str(claimed.project_id)
    reference = library.reference(claimed.project_id)
    assert resource["document_library"] == reference
    assert canonical_json({"resource_key": "outputs", **reference}) in context.prompt
    assert str(claimed.project_id) in context.prompt
    for locator in (str(library.namespace.namespace_id),
                    library.scope(claimed.project_id)["key_prefix"], "storage.example.test"):
        assert locator not in context.prompt
    Draft202012Validator(
        ContractStore(CONTRACTS).load("agent-task-brief/v1.schema.json"),
        format_checker=FormatChecker(),
    ).validate(context.task_brief)


async def test_input_and_library_context_materializes_only_selected_input(tmp_path: Path):
    """実物化と再準備を通し、成果 slot に入力 directory を案内しない。"""

    claimed, library = library_run(with_input=True)
    selected = document_content(name="selected.md")
    hidden = document_content(name="not-selected.md")
    claimed.selected_sources_json["project-doc"] = {
        "capability": "document.read/v1",
        "provider": "project-documents",
        "document_snapshot": document_snapshot(
            claimed.project_id, [selected], key="project-doc"
        ).to_json(),
    }
    inventory = _FakeInventory([selected, hidden])
    runtime = builder(
        tmp_path,
        library,
        materializer=WorkspaceMaterializer(
            document_inventory=inventory,
            input_snapshots=MemoryInputSnapshots(claimed),
            max_bytes=10_485_760,
            max_files=500,
        ),
    )
    context = await runtime.build(claimed, sequence_start=1)
    assert {tool.capability for tool in context.tools} == {"document.read/v1", "change.propose/v1"}
    assert (context.workspace.input_dir / "documents/specs/selected.md").is_file()
    assert not (context.workspace.input_dir / "documents/specs/not-selected.md").exists()
    resources = {item["key"]: item for item in context.task_brief["resources"]}
    assert resources["project-doc"]["materialization"]["root"] == "input/documents"
    assert "materialization" not in resources["outputs"]
    retry = await runtime.build(claimed, sequence_start=20)
    assert retry.resolved_sources == context.resolved_sources
    assert retry.workspace == context.workspace and inventory.calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "disabled",
        "missing-target",
        "changed-target",
        "foreign-run",
        "foreign-project",
        "binding",
        "legacy-binding",
        "hidden-input",
        "extra-slot",
        "missing-selection",
        "no-proposal",
        "unknown-capability",
    ],
)
async def test_invalid_library_context_stops_before_materialization(tmp_path: Path, mutation):
    """不完全/別世代/別 Run の保存先と権限不足を、workspace/外部読取より前に拒否する。"""

    claimed, library = library_run()
    enabled = mutation != "disabled"
    if mutation == "missing-target":
        library = None
    elif mutation == "changed-target":
        library = target()
    elif mutation == "foreign-run":
        claimed = replace(claimed, run_id=uuid4())
    elif mutation == "foreign-project":
        claimed = replace(claimed, project_id=uuid4())
    elif mutation == "binding":
        claimed.selected_sources_json["outputs"]["binding_id"] = str(uuid4())
    elif mutation == "legacy-binding":
        claimed.selected_sources_json["outputs"] = FrozenDocumentLibraryBinding(
            claimed.project_id, claimed.run_id, uuid4(), "outputs", library, revision="1"
        ).to_json()
    elif mutation == "hidden-input":
        claimed.selected_sources_json["outputs"]["document_snapshot"] = {}
    elif mutation == "extra-slot":
        claimed.selected_sources_json["undeclared"] = deepcopy(
            claimed.selected_sources_json["outputs"]
        )
    elif mutation == "missing-selection":
        claimed.selected_sources_json.clear()
    elif mutation == "no-proposal":
        claimed.permission_snapshot_json["allowed_capabilities"].remove("change.propose/v1")
    elif mutation == "unknown-capability":
        claimed.selected_sources_json["outputs"]["capability"] = "document.unknown/v1"
    materializer = AsyncMock(spec=WorkspaceMaterializer)
    with pytest.raises(ValueError):
        await builder(tmp_path, library, enabled=enabled, materializer=materializer).build(
            claimed, sequence_start=1
        )
    materializer.materialize.assert_not_awaited()
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "changes,operation",
    [
        ({"access": "read"}, "CREATE"),
        ({"kind": "other"}, "CREATE"),
        ({"capabilities": ["document.write/v1", "document.read/v1"]}, "CREATE"),
        ({}, "UPDATE"),
    ],
)
async def test_library_requires_exact_document_create_declaration(
    tmp_path: Path, changes, operation
):
    """既存 read/他種別 slot と CREATE 以外の intent に保存 binding を流用しない。"""

    claimed, library = library_run(requirement_changes=changes, operation=operation)
    with pytest.raises(ValueError):
        await builder(tmp_path, library).build(claimed, sequence_start=1)
    assert not (tmp_path / "runs").exists()


def test_brief_preserves_distinct_same_capability_slots_and_unselected_resources():
    """同能力の二資源を provider で上書きせず、未選択 slot へも流用しない。"""

    requirements = [
        {"key": key, "kind": "issue", "access": "read", "capabilities": ["issue.read/v1"]}
        for key in ("first", "second", "unselected")
    ]
    resources = _resources(
        {"resource_requirements": requirements},
        selected_sources={
            "first": {"capability": "issue.read/v1", "provider": "csv"},
            "second": {"capability": "issue.read/v1", "provider": "redmine"},
        },
        run_id=uuid4(), project_id=uuid4(),
    )
    assert [item["binding"]["provider"] for item in resources[:2]] == ["csv", "redmine"]
    assert resources[2]["binding"] is None


@pytest.mark.parametrize("mismatch", ["missing-project", "project", "run", "access"])
def test_brief_library_reference_requires_exact_original_identity(mismatch):
    """Brief の単独組成でも、未確認/別 Run の scope を登録先として公開しない。"""
    claimed, _library = library_run()
    requirement = {"key": "outputs", "kind": "document", "access": "write",
                   "capabilities": ["document.write/v1"]}
    project_id, run_id = claimed.project_id, claimed.run_id
    if mismatch == "missing-project":
        project_id = None
    elif mismatch == "project":
        project_id = uuid4()
    elif mismatch == "run":
        run_id = uuid4()
    else:
        requirement["access"] = "read"
    with pytest.raises(ValueError):
        _resources(
            {"resource_requirements": [requirement]},
            selected_sources=claimed.selected_sources_json,
            project_id=project_id, run_id=run_id,
        )
