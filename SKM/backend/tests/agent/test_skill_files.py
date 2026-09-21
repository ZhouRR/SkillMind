"""凍結 Skill の実物化から read/search/JSON 検証まで、モデルによる再出力なしに通す。"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.contract_store import (
    ContractStore,
)
from skillmind.agent.input_workspace import read_input_file, verify_input
from skillmind.agent.json_schema_provider import JsonSchemaValidateProvider
from skillmind.agent.materialization_storage import MaterializationError
from skillmind.agent.skill_files import SKILL_FILES_ROOT, source_file_contents
from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.agent.workspace_provider import (
    WorkspaceReadProvider,
    WorkspaceSearchProvider,
    WorkspaceWriteProvider,
)
from skillmind.core.hashing import sha256_hex
from skillmind.runs.input_snapshot import InputSnapshotError, InputSnapshotStatus
from tests.agent.input_fakes import MemoryInputSnapshots
from tests.agent.test_runtime_context import (
    CONTRACTS,
    _document_builder,
    _document_manifest,
    _generic_claimed,
    _generic_manifest,
)
from tests.agent.test_workspace_materializer import _FakeInventory
from tests.agent.test_workspace_provider import _context
from tests.documents.fakes import document_content, document_snapshot

SCHEMA_TEXT = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "原 Schema の説明も保持する",
  "$defs": {"count": {"type": "integer", "minimum": 1}},
  "type": "object",
  "properties": {"count": {"$ref": "#/$defs/count"}},
  "required": ["count"],
  "additionalProperties": false
}\r\n"""


def _sources():
    """任意名の Schema・参考資料・コードを一つの原文集合として固定する。"""
    return [
        {"path": path, "content": text, "sha256": "sha256:" + sha256_hex(text.encode("utf-8"))}
        for path, text in [
            ("SKILL.md", "# Generic file task\r\nUse the bundled schema.\r\n"),
            ("schemas/独自.json", SCHEMA_TEXT),
            ("references/说明.txt", "照合だけを行う。\r\n末尾空白を保持  \n"),
            ("scripts/never_run.py", "raise RuntimeError('must remain data')\n"),
        ]
    ]


def _case(
    tmp_path,
    *,
    capabilities=("workspace.read/v1", "workspace.search/v1", "json.schema.validate/v1"),
    documents=None,
    **limits,
):
    """本番 Builder と物化器を共有し、外部 I/O と DB 回执だけを fake にする。"""
    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": c, "required": True} for c in capabilities]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "SUPERVISED"
    }
    manifest["source_documents"] = _sources() if documents is None else documents
    claim = _generic_claimed(
        manifest=manifest, selected_sources={}, required=False, allowed=capabilities
    )
    claim.permission_snapshot_json["execution_profile"] = "SUPERVISED"
    store = MemoryInputSnapshots(claim)
    inventory = _FakeInventory([])
    materializer = WorkspaceMaterializer(
        document_inventory=inventory,
        input_snapshots=store,
        **{"max_bytes": 10_485_760, "max_files": 1000, **limits},
    )
    builder = _document_builder(tmp_path, materializer=materializer)
    return claim, store, inventory, materializer, builder


def _tool_context(tmp_path, context, capability):
    """実 Context の世代と identity を、対応する既存 Provider に渡す。"""
    original = _context(tmp_path, capability)
    return replace(
        original,
        workspace=context.workspace,
        run_id=context.run_id,
        run_attempt_id=context.run_attempt_id,
        project_id=context.project_id,
    )


async def test_builder_seals_exact_source_and_tools_validate_without_copying(tmp_path):
    """CRLF・日本語・末尾空白・Schema 注釈を原 byte で保ち、実子 process で照合する。"""
    claim, store, inventory, _, builder = _case(tmp_path)
    before = deepcopy(claim)
    context = await builder.build(claim, sequence_start=1)
    assert claim == before
    assert inventory.calls == 0
    assert store.record.status is InputSnapshotStatus.READY
    locations = context.task_brief["skill_files"]
    assert len(locations) == len(_sources())
    assert "pass its path directly as schema_path" in context.prompt
    assert "do not rewrite" in context.prompt
    assert {t.capability for t in context.tools} == set(
        claim.permission_snapshot_json["allowed_capabilities"]
    )
    for source in _sources():
        relative = f"{SKILL_FILES_ROOT}/{source['path']}"
        target = context.workspace.input_dir / relative
        raw = source["content"].encode("utf-8")
        assert target.read_bytes() == raw
        assert target.stat().st_mode & 0o222 == 0
        assert read_input_file(context.workspace, relative, max_bytes=1_048_576) == raw
        assert {
            "source_path": source["path"],
            "path": f"input/{relative}",
            "sha256": source["sha256"],
        } in locations
    verify_input(context.workspace)
    assert list(context.workspace.output_dir.iterdir()) == []
    reader = _tool_context(tmp_path, context, "workspace.read/v1")
    result = await WorkspaceReadProvider().execute(
        reader,
        {
            "path": f"input/{SKILL_FILES_ROOT}/references/说明.txt",
            "purpose": "Read frozen source",
        },
    )
    assert "照合だけ" in result.response["content"]
    searched = await WorkspaceSearchProvider().execute(
        _tool_context(tmp_path, context, "workspace.search/v1"),
        {"query": "原 Schema", "paths": [f"input/{SKILL_FILES_ROOT}"], "purpose": "Find source"},
    )
    assert len(searched.response["matches"]) == 1
    for instance, valid in [({"count": 2}, True), ({"count": 0}, False)]:
        (context.workspace.output_dir / "result.json").write_text(json.dumps(instance))
        checked = await JsonSchemaValidateProvider().execute(
            _tool_context(tmp_path, context, "json.schema.validate/v1"),
            {
                "schema_path": f"input/{SKILL_FILES_ROOT}/schemas/独自.json",
                "instance_path": "output/result.json",
            },
        )
        assert checked.response["valid"] is valid
        assert checked.response["schema_hash"] == "sha256:" + sha256_hex(SCHEMA_TEXT.encode())
    assert [p.name for p in context.workspace.output_dir.iterdir()] == ["result.json"]
    with pytest.raises(ToolProviderError):
        await WorkspaceWriteProvider().execute(
            _tool_context(tmp_path, context, "workspace.write/v1"),
            {
                "path": f"input/{SKILL_FILES_ROOT}/schemas/独自.json",
                "content": "{}",
                "purpose": "Cannot replace frozen source",
            },
        )


async def test_reuse_keeps_same_paths_bytes_and_single_ready_receipt(tmp_path, monkeypatch):
    """再訪で再生成せず、公開 file は同じ回执で再検証して渡す。"""
    claim, store, _, _, builder = _case(tmp_path)
    first = await builder.build(claim, sequence_start=1)

    def no_write(*args, **kwargs):
        """READY の再訪で新しい byte が書かれないことを証明する。"""
        raise AssertionError("READY files must not be rewritten")

    monkeypatch.setattr(WorkspaceMaterializer, "_write_tree", no_write)
    second = await builder.build(claim, sequence_start=50)
    assert first.workspace == second.workspace
    assert first.task_brief == second.task_brief
    assert first.task_brief_checksum == second.task_brief_checksum
    assert store.complete_calls == 1


@pytest.mark.parametrize("mutation", ["content", "missing", "extra", "symlink", "hardlink"])
async def test_changed_skill_input_is_rejected_on_reuse(tmp_path, mutation):
    """原文破損・差替え・追加を再物化で隠さず、元の READY と現場を保持する。"""
    claim, store, _, _, builder = _case(tmp_path)
    context = await builder.build(claim, sequence_start=1)
    original = store.record
    path = context.workspace.input_dir / SKILL_FILES_ROOT / "schemas/独自.json"
    if mutation == "content":
        path.chmod(0o600)
        path.write_text("{}")
    else:
        path.parent.chmod(0o700)
        if mutation == "extra":
            (path.parent / "extra.json").write_text("{}")
        else:
            path.unlink()
            outside = tmp_path / "outside.json"
            outside.write_text(SCHEMA_TEXT)
            if mutation == "symlink":
                path.symlink_to(outside)
            elif mutation == "hardlink":
                os.link(outside, path)
    with pytest.raises(MaterializationError):
        await builder.build(claim, sequence_start=5)
    assert store.record == original
    assert store.complete_calls == 1


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", "a\\b", "a//b"])
async def test_invalid_source_paths_fail_before_input_generation(tmp_path, path):
    """原 path の traversal は通常の source 検証で止め、世代を作らない。"""
    docs = _sources()
    docs[0]["path"] = path
    claim, store, _, _, builder = _case(tmp_path, documents=docs)
    with pytest.raises(ValueError):
        await builder.build(claim, sequence_start=1)
    assert store.begin_calls == store.complete_calls == 0


async def test_conflicting_source_files_fail_before_generation(tmp_path):
    """file と directory の衝突を write の途中ではなく事前に拒否する。"""
    docs = _sources()
    docs[0]["path"] = "schemas"
    claim, store, _, _, builder = _case(tmp_path, documents=docs)
    with pytest.raises(InputSnapshotError):
        await builder.build(claim, sequence_start=1)
    assert store.begin_calls == 0


async def test_corrupt_source_hash_cannot_be_materialized(tmp_path):
    """Manifest identity が整っていても原文自身の hash が違えば補签しない。"""
    docs = _sources()
    docs[0]["sha256"] = "sha256:" + "0" * 64
    claim, store, _, _, builder = _case(tmp_path, documents=docs)
    with pytest.raises(ValueError):
        await builder.build(claim, sequence_start=1)
    assert store.begin_calls == 0


@pytest.mark.parametrize(
    "limits", [{"max_total_files": 3}, {"max_total_bytes": 10}, {"max_files": 3}, {"max_bytes": 10}]
)
async def test_optional_skill_files_never_exceed_input_budgets_or_block_run(tmp_path, limits):
    """容量不足は全副本を省略して完全原文を維持する。途中の file は公開しない。"""
    claim, store, _, _, builder = _case(tmp_path, **limits)
    context = await builder.build(claim, sequence_start=1)
    assert context.task_brief["source_documents"] == _sources()
    assert "skill_files" not in context.task_brief
    assert not (context.workspace.input_dir / ".skillmind").exists()
    assert store.record.status is InputSnapshotStatus.READY
    assert store.record.files == ()
    resumed = await builder.build(claim, sequence_start=10)
    assert resumed.task_brief == context.task_brief
    assert store.complete_calls == 1


@pytest.mark.parametrize("capabilities", [(), ("json.schema.validate/v1",)])
async def test_schema_tool_can_read_source_without_adding_workspace_permissions(
    tmp_path,
    capabilities,
):
    """既存 schema Tool 単独でも物化し、読取 Tool のない Run へ権限を増やさない。"""
    claim, store, _, _, builder = _case(tmp_path, capabilities=capabilities)
    context = await builder.build(claim, sequence_start=1)
    assert bool(context.task_brief.get("skill_files")) is bool(capabilities)
    assert [t.capability for t in context.tools] == list(capabilities)
    assert bool(store.record.files) is bool(capabilities)


async def test_no_materializer_never_advertises_a_guessed_path(tmp_path):
    """未配線の context は原文だけを渡し、存在しない file を案内しない。"""
    claim, _, _, _, _ = _case(tmp_path)
    context = await _document_builder(tmp_path).build(claim, sequence_start=1)
    assert "skill_files" not in context.task_brief
    assert "input/.skillmind/skill" not in context.prompt


async def test_existing_ready_input_is_not_extended_or_blocked(tmp_path):
    """完了済み input に後付けを行わず、従来の原文経路を維持する。"""
    claim, store, _, materializer, builder = _case(tmp_path)
    workspace = WorkspaceManager((tmp_path / "runs").resolve()).initialize(claim.run_id)
    old = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=claim.project_id,
        run_id=claim.run_id,
        blueprint=claim.skill_snapshots_json[0]["manifest"]["capability_blueprint"],
    )
    assert old.workspace.input_files == ()
    original = store.record
    context = await builder.build(claim, sequence_start=1)
    assert "skill_files" not in context.task_brief
    assert store.record == original and store.complete_calls == 1
    assert context.task_brief["source_documents"] == _sources()


async def test_materializer_rejects_sources_from_another_manifest(tmp_path):
    """呼出し側の正しい hash だけで別 Skill の text を混入させない。"""
    claim, store, _, materializer, _ = _case(tmp_path)
    docs = _sources()
    docs[0]["content"] = "another source"
    docs[0]["sha256"] = "sha256:" + sha256_hex(docs[0]["content"])
    workspace = WorkspaceManager((tmp_path / "runs").resolve()).initialize(claim.run_id)
    with pytest.raises(MaterializationError):
        await materializer.materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=claim.project_id,
            run_id=claim.run_id,
            blueprint={},
            skill_documents=docs,
        )
    assert store.begin_calls == 0


async def test_cancellation_before_complete_does_not_publish_skill_locations(tmp_path):
    """遅い I/O 後に取り消された世代を READY にせず、Agent へ渡さない。"""
    claim, store, _, materializer, builder = _case(tmp_path)
    original = materializer._write_tree

    def cancel_after_write(*args, **kwargs):
        """実書込後に現在の実行権を取消す。"""
        original(*args, **kwargs)
        store.cancelled = True

    materializer._write_tree = cancel_after_write
    with pytest.raises(MaterializationError, match="cancelled"):
        await builder.build(claim, sequence_start=1)
    assert store.record.status is InputSnapshotStatus.PREPARING


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_brief_contract_and_checksum_include_only_verified_locations(version):
    """旧・原文 Brief とも optional 配置を同じ形で検証し、本文を重複しない。"""
    from skillmind.agent.domain import RunLimits
    from tests.agent.test_task_brief import _blueprint, _manifest, _task_snapshot
    from tests.skills.test_source_execution import compile_case

    if version == "v1":
        manifest = _manifest(blueprint=_blueprint())
        snapshot = _task_snapshot(manifest)
    else:
        _, manifest = compile_case()
        snapshot = {
            **manifest["tasks"][0],
            "task_key": "execute",
            "skill_version_id": str(uuid4()),
            "manifest_checksum": "sha256:" + "a" * 64,
            "output_schema_checksum": "sha256:" + "b" * 64,
        }
    manifest["source_documents"] = _sources()
    files = tuple(seal for seal, _ in source_file_contents(_sources()))
    args = dict(
        run_id=uuid4(),
        task_snapshot=snapshot,
        manifest=manifest,
        selected_sources={},
        tools=[],
        limits=RunLimits(max_turns=20, wall_timeout_seconds=300, max_output_bytes=100000),
    )
    without = build_agent_task_brief(**args)
    with_files = build_agent_task_brief(**args, skill_files=files)
    schema = ContractStore(CONTRACTS).load(f"agent-task-brief/{version}.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(with_files.brief)
    assert with_files.checksum != without.checksum
    assert with_files.brief["source_documents"] == _sources()
    assert all(
        set(item) == {"source_path", "path", "sha256"} for item in with_files.brief["skill_files"]
    )
    prompt = render_task_brief_prompt(with_files.brief, input_json={}, output_schema={})
    assert f"input/{SKILL_FILES_ROOT}/schemas/独自.json" in prompt
    with pytest.raises(MaterializationError):
        build_agent_task_brief(**args, skill_files=files[:-1])


async def test_skill_sources_coexist_with_selected_documents_across_attempts(tmp_path):
    """文書 input と Skill は同じ封印に入り、新 Attempt も再取得・再物化しない。"""
    selected = document_content(name="selected.md")
    hidden = document_content(name="not-selected.md")
    manifest = _document_manifest()
    manifest["source_documents"] = _sources()
    capabilities = ("document.read/v1", "workspace.read/v1", "json.schema.validate/v1")
    manifest["tools"] = [{"capability": c, "required": True} for c in capabilities]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "SUPERVISED"
    }
    claim = _generic_claimed(manifest=manifest, allowed=capabilities, selected_sources={})
    claim.permission_snapshot_json["execution_profile"] = "SUPERVISED"
    claim.selected_sources_json["project-doc"] = {
        "capability": "document.read/v1",
        "provider": "project-documents",
        "document_snapshot": document_snapshot(
            claim.project_id, [selected], key="project-doc"
        ).to_json(),
    }
    store = MemoryInputSnapshots(claim)
    inventory = _FakeInventory([selected, hidden])
    builder = _document_builder(
        tmp_path,
        materializer=WorkspaceMaterializer(
            document_inventory=inventory,
            input_snapshots=store,
            max_bytes=10_485_760,
            max_files=1000,
        ),
    )
    first = await builder.build(claim, sequence_start=1)
    assert (first.workspace.input_dir / "documents/specs/selected.md").is_file()
    assert not (first.workspace.input_dir / "documents/specs/not-selected.md").exists()
    assert "input/documents/.skillmind/files.txt" in first.prompt
    assert len(first.task_brief["skill_files"]) == len(_sources())
    verify_input(first.workspace)
    next_claim = replace(claim, run_attempt_id=uuid4(), attempt_no=2, lease_token="next-owner")
    store.current_claim = deepcopy(next_claim)
    resumed = await builder.build(next_claim, sequence_start=50)
    assert resumed.workspace == first.workspace
    assert resumed.task_brief["skill_files"] == first.task_brief["skill_files"]
    assert inventory.calls == store.complete_calls == 1
    assert store.record.prepared_by_attempt_id == claim.run_attempt_id


async def test_valid_import_larger_than_workspace_file_limit_keeps_original_prompt(tmp_path):
    """CLI で許可される大きな原文を、新しい副本の上限で実行不能にしない。"""
    text = "x" * 1_048_577
    docs = [
        {"path": "references/large.txt", "content": text, "sha256": "sha256:" + sha256_hex(text)}
    ]
    claim, store, _, _, builder = _case(tmp_path, documents=docs)
    context = await builder.build(claim, sequence_start=1)
    assert "skill_files" not in context.task_brief
    assert context.task_brief["source_documents"] == docs
    assert store.record.status is InputSnapshotStatus.READY
    assert store.record.files == ()
