"""Workspace、fixture Provider、ProductionRunContextBuilder の境界を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.context_builder import (
    ContractStore,
    ProductionRunContextBuilder,
    _read_tool_definitions,
    create_run_tool_registry,
)
from skillmind.agent.domain import RunWorkspace
from skillmind.agent.repository_source import RepositorySnapshotSource
from skillmind.agent.tool_gateway import RunToolContext, ToolProviderError
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.source import ProjectDocumentContent
from skillmind.integrations.domain import ResourceBindingLevel, binding_checksum
from skillmind.runs.domain import ClaimedRun
from tests.agent.fixture_providers import CsvFixtureIssueProvider, GitFixtureRepositoryProvider
from tests.agent.fixture_registry import PROVIDER_FIXTURES, create_fixture_tool_registry
from tests.agent.input_fakes import MemoryInputSnapshots
from tests.agent.test_workspace_materializer import _FakeInventory
from tests.documents.fakes import document_content, document_snapshot

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


@pytest.mark.parametrize(
    ("capability", "provider"),
    [
        ("issue.read/v1", "csv"),
        ("issue.read/v1", "redmine"),
        ("repository.read/v1", "git"),
        ("repository.read/v1", "svn"),
        ("database.read/v1", "postgres"),
        ("mcp.read/v1", "mcp"),
    ],
)
def test_production_registry_requires_explicit_read_clients(
    capability: str,
    provider: str,
) -> None:
    """Client 未設定の起動は可能だが、合成 data を利用する能力は公開しない。"""

    registry = create_run_tool_registry(
        ContractStore(CONTRACTS), document_source=_NoopDocumentSource()
    )
    with pytest.raises(LookupError, match="not installed"):
        registry.resolve(capability, provider=provider, integration_id=None)


@pytest.mark.asyncio
async def test_production_repository_wiring_never_falls_back_to_fixture() -> None:
    """実 client を登録しても未束縛 Tool は I/O 前に失敗し、固定 tree を返さない。"""

    source = Mock()
    definitions = _read_tool_definitions(
        ContractStore(CONTRACTS), repository_source=cast(RepositorySnapshotSource, source)
    )
    assert len(definitions) == 1
    definition = definitions[0]
    assert set(definition.providers) == {"git", "svn"}
    with pytest.raises(ToolProviderError, match="binding is invalid"):
        await definition.providers["git"].execute(
            _provider_context("repository.read/v1", "git"),
            {"revision": "1" * 40, "path": "src/example.py"},
        )
    source.open.assert_not_called()


def test_production_issue_wiring_never_installs_csv_fixture() -> None:
    """Redmine を注入しても CSV の test Provider は本番 registry へ混入しない。"""

    definitions = _read_tool_definitions(
        ContractStore(CONTRACTS), redmine_issue_provider=Mock()
    )
    assert len(definitions) == 1
    assert set(definitions[0].providers) == {"redmine"}


def _provider_context(capability: str, provider: str) -> RunToolContext:
    """Provider 単体契約検証用の Run identity を返す。"""

    contracts = ContractStore(CONTRACTS)
    registered = create_fixture_tool_registry(contracts).resolve(
        capability, provider=provider, integration_id=None
    )
    run_id = uuid4()
    root = Path("/tmp/skillmind-provider-tests") / str(run_id)
    return RunToolContext(
        run_id=run_id,
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        tool=registered,
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
    )


def _validate_provider_response(relative_schema: str, response: dict[str, Any]) -> None:
    """Gateway が付与する Evidence ref を補い、公開 Schema と照合する。"""

    payload = dict(response)
    payload["evidence_refs"] = ["ev_fixture_001"]
    schema = ContractStore(CONTRACTS).load(relative_schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


def test_workspace_manager_creates_stable_run_directories(tmp_path: Path) -> None:
    """Retry 時も同じ root を再利用し、必要 directory だけを作成する。"""

    manager = WorkspaceManager((tmp_path / "runs").resolve())
    run_id = uuid4()
    first = manager.initialize(run_id)
    second = manager.initialize(run_id)

    assert first == second
    assert all(
        path.is_dir()
        for path in (first.root, first.cwd, first.input_dir, first.output_dir, first.temp_dir)
    )


def test_workspace_manager_rejects_file_boundary(tmp_path: Path) -> None:
    """Workspace root のすり替えを directory 作成前に拒否する。"""

    root = tmp_path / "runs"
    root.write_text("not-a-directory", encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        WorkspaceManager(root.resolve()).initialize(uuid4())


@pytest.mark.asyncio
async def test_csv_fixture_provider_matches_issue_contract() -> None:
    """CSV fixture が Ticket 契約と再現可能な Evidence を返す。"""

    provider = CsvFixtureIssueProvider(PROVIDER_FIXTURES / "issues.csv")
    result = await provider.execute(
        _provider_context("issue.read/v1", "csv"),
        {"issue_ref": "fixture-001", "purpose": "test"},
    )
    _validate_provider_response("tools/issue.read/v1/response.schema.json", dict(result.response))
    assert result.evidence[0].source_uri == "csv://fixture/issues/fixture-001"

    with pytest.raises(ToolProviderError) as failure:
        await provider.execute(
            _provider_context("issue.read/v1", "csv"),
            {"issue_ref": "missing", "purpose": "test"},
        )
    assert failure.value.code == "not_found"


@pytest.mark.asyncio
async def test_git_fixture_provider_matches_repository_contract() -> None:
    """Git fixture が固定 revision と file hash 付きで source を返す。"""

    root = PROVIDER_FIXTURES / "repository"
    provider = GitFixtureRepositoryProvider(root)
    revision = (root / "revision.txt").read_text(encoding="utf-8").strip()
    result = await provider.execute(
        _provider_context("repository.read/v1", "git"),
        {
            "revision": revision,
            "path": "src/example.py",
            "line_start": 1,
            "line_end": 4,
            "purpose": "test",
        },
    )
    _validate_provider_response(
        "tools/repository.read/v1/response.schema.json", dict(result.response)
    )
    assert result.response["revision"] == revision
    assert result.evidence[0].source_uri.startswith("git://fixture/")


@pytest.mark.asyncio
async def test_context_builder_resolves_fixture_tools_and_workspace(tmp_path: Path) -> None:
    """Claim snapshot から CSV/Git 以外を公開しない RunContext を構築する。"""

    contracts = ContractStore(CONTRACTS)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(contracts),
        model="claude-test",
    )
    claimed = _generic_claimed(
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "csv"}}
    )
    context = await builder.build(claimed, sequence_start=7)

    assert context.project_id == claimed.project_id
    assert context.user_id == claimed.actor_id
    assert context.sequence_start == 7
    assert [tool.capability for tool in context.tools] == ["issue.read/v1"]
    assert context.workspace.root.is_dir()
    assert "Return ONLY one JSON object" in context.prompt
    assert "Do not use Markdown" in context.prompt
    assert '"summary"' in context.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability", ["subagent.dispatch/v1", "change.propose/v1", "issue.update/v1"]
)
async def test_readonly_deployment_rejects_legacy_permissions_before_io(
    tmp_path: Path, capability: str
) -> None:
    """任意 Tool の欠落を黙って無視せず、旧 permission 全体を物化前に拒否する。"""
    materializer = Mock()
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(ContractStore(CONTRACTS)),
        model="claude-test",
        materializer=materializer,
        deferred_features_enabled=False,
    )
    claimed = _generic_claimed(selected_sources={}, allowed=(capability,))
    with pytest.raises(ValueError, match="disabled execution features"):
        await builder.build(claimed, sequence_start=1)
    materializer.materialize.assert_not_called()
    assert not (tmp_path / "runs").exists()


@pytest.mark.asyncio
async def test_context_builder_rejects_uninstalled_production_source(tmp_path: Path) -> None:
    """Test registry に未登録の実 Provider を選んでも dispatch を拒否する。"""

    contracts = ContractStore(CONTRACTS)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(contracts),
        model="claude-test",
    )
    manifest = _generic_manifest()
    manifest["capability_blueprint"]["resource_requirements"][0]["accepted_providers"] = [
        "csv",
        "redmine",
    ]
    with pytest.raises(LookupError, match="Provider is not installed"):
        await builder.build(
            _generic_claimed(
                manifest=manifest,
                selected_sources={
                    "primary-issues": {
                        "capability": "issue.read/v1",
                        "provider": "redmine",
                    }
                },
            ),
            sequence_start=1,
        )


@pytest.mark.asyncio
async def test_context_builder_requires_anthropic_model_at_dispatch(tmp_path: Path) -> None:
    """Worker 起動は許可しつつ、model 未設定の実 Run は閉じて失敗する。"""

    contracts = ContractStore(CONTRACTS)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(contracts),
        model=None,
    )

    with pytest.raises(ValueError, match="ANTHROPIC_MODEL must be configured"):
        await builder.build(_generic_claimed(selected_sources={}), sequence_start=1)


def _generic_manifest(*, required: bool = True) -> dict[str, Any]:
    """issue.read/v1 を要求する Generated Schema 付き published Manifest を作る。"""

    return {
        "manifest_version": "skillmind/v1alpha1",
        "identity": {"skill_key": "generic-repository-review"},
        "compatibility": {"level": "adapted", "confidence": 0.9, "diagnostics": []},
        "capabilities": [
            {"key": "generic.repository.review/v1", "title": "Generic repository review"}
        ],
        "tasks": [
            {
                "key": "review",
                "capability": "generic.repository.review/v1",
                "type": "immediate",
                "input_schema": {
                    "type": "object",
                    "required": ["target_path"],
                    "properties": {"target_path": {"type": "string"}},
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                    "additionalProperties": False,
                },
                "workflow": "review-v1",
                "view": "generic-structured",
            }
        ],
        "tools": [{"capability": "issue.read/v1", "required": required}],
        # Run が凍結する manifest は発行 gate 通過済みであり、必ず蓝图を持つ。Brief はこれを
        # 唯一の guidance 源として読むため、fixture 側も同じ形を保つ。
        "capability_blueprint": {
            "blueprint_version": "skillmind.capability-blueprint/v1",
            "identity": {
                "skill_key": "generic-repository-review",
                "source_hash": "sha256:" + ("a" * 64),
                "interpretation_id": "00000000-0000-4000-8000-000000000123",
                "interpreter_version": "skillmind-skill-interpreter/2.3.0",
            },
            "compatibility": {"level": "adapted"},
            "capabilities": [
                {"key": "generic.repository.review", "title": "Generic repository review"}
            ],
            "tasks": [
                {
                    "key": "review",
                    "capability": "generic.repository.review",
                    "objective": "Review the generic repository target and report findings.",
                }
            ],
            "resource_requirements": [
                {
                    "key": "primary-issues",
                    "kind": "issue",
                    "required": required,
                    "access": "read",
                    "capabilities": ["issue.read/v1"],
                    "accepted_providers": ["csv"],
                }
            ],
            "guidance": {
                "required_rules": [],
                "recommended_steps": [],
                "quality_criteria": [],
                "prohibited_actions": [],
            },
            "source_traces": [
                {
                    "target": "/tasks/0",
                    "path": "SKILL.md",
                    "line": 5,
                    "reason": "The Goal section defines the review objective.",
                }
            ],
        },
    }


def _generic_claimed(
    *,
    selected_sources: dict[str, Any],
    required: bool = True,
    allowed: tuple[str, ...] = ("issue.read/v1",),
    manifest: dict[str, Any] | None = None,
) -> ClaimedRun:
    """汎用の published task から作られる通用 Run の claim を組み立てる。

    checksum は Manifest 実体から算出し、context builder の凍結内容照合を通過させる。
    """

    actor_id = uuid4()
    manifest = manifest if manifest is not None else _generic_manifest(required=required)
    checksum = f"sha256:{sha256_hex(canonical_json(manifest))}"
    skill_version_id = uuid4()
    skill_snapshot = {
        "skill_version_id": str(skill_version_id),
        "manifest_checksum": checksum,
        "manifest": manifest,
    }
    claimed = ClaimedRun(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        actor_id=actor_id,
        attempt_no=1,
        lease_token="lease-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        row_version=2,
        input_json={"target_path": "src/example.py"},
        task_snapshot_json={
            # 実 Run は task_key を凍結する (runs/service.py)。Brief は蓝图 task をこの key で
            # 引くため、fixture が省くと目標が manifest 由来の退避値に落ちて検証が空振りする。
            "task_key": manifest["tasks"][0]["key"],
            "capability": manifest["tasks"][0]["capability"],
            "input_schema": "sha256:" + ("b" * 64),
            "output_schema": "sha256:" + ("c" * 64),
            "input_schema_json": manifest["tasks"][0]["input_schema"],
            "output_schema_json": manifest["tasks"][0]["output_schema"],
            "skill_version_id": str(skill_version_id),
            "manifest_checksum": checksum,
        },
        permission_snapshot_json={
            "mode": "auto_read_only",
            "actor_id": str(actor_id),
            "allowed_capabilities": list(allowed),
        },
        selected_sources_json=selected_sources,
        limits_snapshot_json={
            "max_turns": 20,
            "wall_timeout_seconds": 900,
            "max_output_bytes": 1_048_576,
        },
        skill_snapshots_json=(skill_snapshot,),
    )
    # 合成 Provider を使う test も実 Run と同じ凍結 identity/検証契約を通す。
    for requirement in manifest["capability_blueprint"]["resource_requirements"]:
        source = selected_sources.get(requirement["key"])
        if not isinstance(source, dict) or requirement["kind"] == "document":
            continue
        source.update({
            "integration_id": str(uuid4()),
            "binding_id": str(uuid4()),
            "binding_capability": source["capability"],
            "revision": "1",
            "resource_kind": requirement["kind"],
            "access": "read",
            "scope": {},
        })
        source["binding_checksum"] = binding_checksum(
            project_id=claimed.project_id,
            scope_level=ResourceBindingLevel.RUN,
            scope_key=str(claimed.run_id),
            requirement_key=requirement["key"],
            resource_kind=requirement["kind"],
            integration_id=UUID(source["integration_id"]),
            provider=source["provider"],
            capability_version=source["capability"],
            revision="1",
            scope={},
        )
    return claimed


def _generic_builder(tmp_path: Path) -> ProductionRunContextBuilder:
    """通用 task 実行検証用の context builder を組み立てる。"""

    contracts = ContractStore(CONTRACTS)
    return ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(contracts),
        model="claude-test",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,provider_name,provider_keyword", [
    ("database.read/v1", "postgres", "database_provider"),
    ("mcp.read/v1", "mcp", "mcp_provider"),
])
async def test_context_builder_resolves_readonly_other_resource(
    tmp_path: Path, capability: str, provider_name: str, provider_keyword: str,
) -> None:
    """公開 other 資源から読取 Tool を原 binding に束縛し、暗黙 I/O は行わない。"""
    manifest = _generic_manifest(required=True)
    manifest["tools"] = [{"capability": capability, "required": True}]
    manifest["capability_blueprint"]["resource_requirements"] = [{
        "key": "reports", "kind": "other", "required": True, "access": "read",
        "capabilities": [capability], "accepted_providers": [provider_name],
    }]
    claimed = _generic_claimed(
        selected_sources={"reports": {"capability": capability, "provider": provider_name}},
        allowed=(capability,),
        manifest=manifest,
    )
    provider = Mock()
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_run_tool_registry(
            ContractStore(CONTRACTS), document_source=_NoopDocumentSource(),
            **{provider_keyword: provider},
        ),
        model="claude-test",
    )
    context = await builder.build(claimed, sequence_start=1)
    assert len(context.tools) == 1
    tool = context.tools[0]
    selected = claimed.selected_sources_json["reports"]
    assert (tool.capability, tool.provider) == (capability, provider_name)
    assert str(tool.integration_id) == selected["integration_id"]
    assert str(tool.binding_id) == selected["binding_id"]
    provider.execute.assert_not_called()


@pytest.mark.asyncio
async def test_context_builder_resolves_generic_structured_source(tmp_path: Path) -> None:
    """汎用 task と構造化 selected_sources から Tool と汎用 prompt を解決する。"""

    builder = _generic_builder(tmp_path)
    claimed = _generic_claimed(
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "csv"}}
    )
    context = await builder.build(claimed, sequence_start=1)

    assert [tool.capability for tool in context.tools] == ["issue.read/v1"]
    assert context.tools[0].provider == "csv"
    # 目標は Manifest が凍結した蓝图そのものから来る。業務固有 固定文言は含まない。
    assert context.prompt.startswith(
        "Objective: Review the generic repository target and report findings."
    )
    assert "issue.read/v1" in context.prompt
    assert "Return ONLY one JSON object" in context.prompt


@pytest.mark.asyncio
async def test_context_builder_uses_frozen_generated_schemas(tmp_path: Path) -> None:
    """Generated Schema は platform file ではなく Run snapshot から実行する。"""

    claimed = _generic_claimed(
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "csv"}}
    )
    claimed.task_snapshot_json["input_schema_json"] = {
        "type": "object",
        "required": ["target_path"],
        "properties": {"target_path": {"type": "string"}},
    }
    claimed.task_snapshot_json["output_schema_json"] = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    }

    context = await _generic_builder(tmp_path).build(claimed, sequence_start=1)

    assert context.result_schema == claimed.task_snapshot_json["output_schema_json"]


@pytest.mark.asyncio
async def test_context_builder_opens_registered_workspace_read_and_search(
    tmp_path: Path,
) -> None:
    """SUPERVISED Run だけが明示宣言した隔離 workspace Tool を取得する。"""

    manifest = _generic_manifest(required=False)
    manifest["tools"] = [
        {"capability": "workspace.read/v1", "required": True},
        {"capability": "workspace.search/v1", "required": True},
    ]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "SUPERVISED"
    }
    claimed = _generic_claimed(
        selected_sources={},
        required=False,
        allowed=("workspace.read/v1", "workspace.search/v1"),
        manifest=manifest,
    )
    claimed.permission_snapshot_json["execution_profile"] = "SUPERVISED"
    contracts = ContractStore(CONTRACTS)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_run_tool_registry(
            contracts,
            document_source=_NoopDocumentSource(),
        ),
        model="claude-test",
    )

    context = await builder.build(claimed, sequence_start=1)

    assert [tool.capability for tool in context.tools] == [
        "workspace.read/v1",
        "workspace.search/v1",
    ]
    assert all(tool.provider == "workspace" for tool in context.tools)
    assert "workspace.search/v1" in context.prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["workspace.write/v1", "workspace.write/v2"])
async def test_context_builder_opens_registered_workspace_write(
    tmp_path: Path, capability: str,
) -> None:
    """SUPERVISED Run は明示された精確版だけを取得し、v1 を暗黙に v2 へ上げない。"""

    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": capability, "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "SUPERVISED"
    }
    claimed = _generic_claimed(
        selected_sources={},
        required=False,
        allowed=(capability,),
        manifest=manifest,
    )
    claimed.permission_snapshot_json["execution_profile"] = "SUPERVISED"
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_run_tool_registry(
            ContractStore(CONTRACTS),
            document_source=_NoopDocumentSource(),
        ),
        model="claude-test",
    )

    context = await builder.build(claimed, sequence_start=1)

    assert [tool.capability for tool in context.tools] == [capability]
    assert context.tools[0].provider == "workspace"


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["workspace.search/v1", "workspace.write/v2"])
async def test_context_builder_denies_workspace_search_below_profile_or_without_snapshot(
    tmp_path: Path,
    capability: str,
) -> None:
    """Skill 宣言だけでは profile 上限や旧 permission snapshot を越えて検索権限を得られない。"""

    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": capability, "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {"recommended_profile": "GUIDED"}
    contracts = ContractStore(CONTRACTS)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_run_tool_registry(
            contracts,
            document_source=_NoopDocumentSource(),
        ),
        model="claude-test",
    )
    guided = _generic_claimed(
        selected_sources={},
        required=False,
        allowed=(capability,),
        manifest=manifest,
    )
    guided.permission_snapshot_json["execution_profile"] = "GUIDED"
    with pytest.raises(LookupError, match="unavailable for the execution profile"):
        await builder.build(guided, sequence_start=1)

    historical = _generic_claimed(
        selected_sources={},
        required=False,
        allowed=(capability,),
        manifest=manifest,
    )
    with pytest.raises(ValueError, match="does not authorize workspace"):
        await builder.build(historical, sequence_start=1)


@pytest.mark.asyncio
async def test_workspace_v1_permission_does_not_authorize_v2_manifest(tmp_path: Path) -> None:
    """版番号の似た能力を代替せず、拡権を workspace 作成より前に拒否する。"""

    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": "workspace.write/v2", "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "SUPERVISED",
    }
    claimed = _generic_claimed(
        selected_sources={}, required=False, allowed=("workspace.write/v1",), manifest=manifest,
    )
    claimed.permission_snapshot_json["execution_profile"] = "SUPERVISED"
    root = (tmp_path / "runs").resolve()
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(root),
        tool_registry=create_run_tool_registry(
            ContractStore(CONTRACTS), document_source=_NoopDocumentSource(),
        ),
        model="claude-test",
    )
    with pytest.raises(ValueError, match="not allowed by the permission snapshot"):
        await builder.build(claimed, sequence_start=1)
    assert not (root / str(claimed.run_id)).exists()


@pytest.mark.asyncio
async def test_context_builder_rejects_legacy_run_without_frozen_schema(tmp_path: Path) -> None:
    """Schema snapshot のない historical Run は読取専用とし再実行を拒否する。"""

    claimed = _generic_claimed(selected_sources={})
    claimed.task_snapshot_json.pop("input_schema_json")

    with pytest.raises(ValueError, match="missing frozen Schema"):
        await _generic_builder(tmp_path).build(claimed, sequence_start=1)


class _NoopDocumentSource:
    """Resolution 検証用の、内容を返さない ProjectDocumentSource。"""

    async def fetch(self, *, project_id: Any, document_id: Any) -> ProjectDocumentContent | None:
        """解決段階では呼ばれないため常に None を返す。"""

        del project_id, document_id
        return None


def _document_manifest() -> dict[str, Any]:
    """document.read/v1 を data source に要求する最小 published Manifest を作る。"""

    return {
        "manifest_version": "skillmind/v1alpha1",
        "identity": {"skill_key": "project-document-review"},
        "compatibility": {"level": "adapted", "confidence": 0.9, "diagnostics": []},
        "capabilities": [{"key": "document.review/v1", "title": "Project document review"}],
        "tasks": [
            {
                "key": "review",
                "capability": "document.review/v1",
                "type": "immediate",
                "input_schema": {
                    "type": "object",
                    "required": ["target_path"],
                    "properties": {"target_path": {"type": "string"}},
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                    "additionalProperties": False,
                },
                "workflow": "review-v1",
                "view": "generic-structured",
            }
        ],
        "tools": [{"capability": "document.read/v1", "required": True}],
        "capability_blueprint": {
            "blueprint_version": "skillmind.capability-blueprint/v1",
            "identity": {
                "skill_key": "project-document-review",
                "source_hash": "sha256:" + ("a" * 64),
                "interpretation_id": "00000000-0000-4000-8000-000000000123",
                "interpreter_version": "skillmind-skill-interpreter/2.3.0",
            },
            "compatibility": {"level": "adapted"},
            "capabilities": [{"key": "document.review", "title": "Project document review"}],
            "tasks": [
                {
                    "key": "review",
                    "capability": "document.review",
                    "objective": "Review the bound project document and report findings.",
                }
            ],
            "resource_requirements": [
                {
                    "key": "project-doc",
                    "kind": "document",
                    "required": True,
                    "access": "read",
                    "capabilities": ["document.read/v1"],
                    "accepted_providers": ["project"],
                }
            ],
            "guidance": {
                "required_rules": [],
                "recommended_steps": [],
                "quality_criteria": [],
                "prohibited_actions": [],
            },
            "source_traces": [
                {
                    "target": "/tasks/0",
                    "path": "SKILL.md",
                    "line": 5,
                    "reason": "The Goal section defines the review objective.",
                }
            ],
        },
    }


def _document_builder(
    tmp_path: Path, *, materializer: WorkspaceMaterializer | None = None
) -> ProductionRunContextBuilder:
    """document.read/v1 を含む registry で通用 context builder を組み立てる。"""

    contracts = ContractStore(CONTRACTS)
    return ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_run_tool_registry(contracts, document_source=_NoopDocumentSource()),
        model="claude-test",
        materializer=materializer,
    )


@pytest.mark.asyncio
async def test_context_builder_resolves_project_document_source(tmp_path: Path) -> None:
    """document.read/v1 を選択した Run が project 文書 provider を解決する (離線解析層)。"""

    builder = _document_builder(tmp_path)
    claimed = _generic_claimed(
        manifest=_document_manifest(),
        allowed=("document.read/v1",),
        selected_sources={},
    )
    claimed.selected_sources_json["project-doc"] = {
        "capability": "document.read/v1",
        "provider": "project-documents",
        "document_snapshot": document_snapshot(
            claimed.project_id, [document_content()], key="project-doc"
        ).to_json(),
    }
    context = await builder.build(claimed, sequence_start=1)

    assert [tool.capability for tool in context.tools] == ["document.read/v1"]
    assert context.tools[0].provider == "project-documents"


async def test_legacy_document_context_cannot_implicitly_authorize_all(tmp_path: Path) -> None:
    """旧 provider 名だけの Run を Project 全文書の認可へ昇格させない。"""

    claimed = _generic_claimed(
        manifest=_document_manifest(),
        allowed=("document.read/v1",),
        selected_sources={"project-doc": {"capability": "document.read/v1", "provider": "project"}},
    )
    with pytest.raises(ValueError):
        await _document_builder(tmp_path).build(claimed, sequence_start=1)


async def test_context_builder_materializes_document_union_and_registers_one_tool(
    tmp_path: Path,
) -> None:
    """実 builder と物化器を通し、複数 slot の集合が一つの Tool と Brief に接続する。"""

    first = document_content(name="selected.md")
    second = document_content(name="reference.md")
    hidden = document_content(name="not-selected.md")
    manifest = _document_manifest()
    requirement = manifest["capability_blueprint"]["resource_requirements"][0]
    manifest["capability_blueprint"]["resource_requirements"].append(
        {**requirement, "key": "reference-doc"}
    )
    claimed = _generic_claimed(
        manifest=manifest, allowed=("document.read/v1",), selected_sources={}
    )
    for key, contents in (("project-doc", [first]), ("reference-doc", [first, second])):
        claimed.selected_sources_json[key] = {
            "capability": "document.read/v1",
            "provider": "project-documents",
            "document_snapshot": document_snapshot(claimed.project_id, contents, key=key).to_json(),
        }
    inventory = _FakeInventory([first, second, hidden])
    builder = _document_builder(
        tmp_path,
        materializer=WorkspaceMaterializer(
            document_inventory=inventory,
            input_snapshots=MemoryInputSnapshots(claimed),
            max_bytes=10_485_760,
            max_files=500,
        ),
    )
    context = await builder.build(claimed, sequence_start=1)
    assert len(context.tools) == 1
    assert context.tools[0].capability == "document.read/v1"
    assert (context.workspace.input_dir / "documents/specs/selected.md").is_file()
    assert (context.workspace.input_dir / "documents/specs/reference.md").is_file()
    assert not (context.workspace.input_dir / "documents/specs/not-selected.md").exists()
    assert "input/documents/.skillmind/files.txt" in context.prompt
    retry = await builder.build(claimed, sequence_start=10)
    assert retry.workspace == context.workspace
    assert inventory.calls == 1


@pytest.mark.asyncio
async def test_context_builder_omits_optional_unselected_source(tmp_path: Path) -> None:
    """任意 data source が未選択なら Tool を公開せず、Tool 無し prompt を返す。"""

    builder = _generic_builder(tmp_path)
    context = await builder.build(
        _generic_claimed(selected_sources={}, required=False, allowed=()), sequence_start=1
    )

    assert context.tools == ()
    assert "No external data-source tools are available" in context.prompt


@pytest.mark.asyncio
async def test_context_builder_rejects_required_unselected_source(tmp_path: Path) -> None:
    """必須 data source が未選択なら実行前に fail closed とする。"""

    builder = _generic_builder(tmp_path)
    with pytest.raises(ValueError, match="Required data source has no selected provider"):
        await builder.build(_generic_claimed(selected_sources={}), sequence_start=1)


@pytest.mark.asyncio
async def test_context_builder_rejects_capability_outside_allowed(tmp_path: Path) -> None:
    """解決した Tool capability が permission snapshot 外なら拒否する。"""

    builder = _generic_builder(tmp_path)
    claimed = _generic_claimed(
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "csv"}},
        allowed=(),
    )
    with pytest.raises(ValueError, match="not allowed by the permission snapshot"):
        await builder.build(claimed, sequence_start=1)


@pytest.mark.asyncio
async def test_context_builder_rejects_provider_without_frozen_binding(tmp_path: Path) -> None:
    """Provider 名だけの履歴を実行可能な資源へ昇格させない。"""

    builder = _generic_builder(tmp_path)
    claimed = _generic_claimed(
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "redmine"}}
    )
    claimed.selected_sources_json["primary-issues"].pop("integration_id")
    with pytest.raises(ValueError, match="requires a frozen Integration binding"):
        await builder.build(claimed, sequence_start=1)
