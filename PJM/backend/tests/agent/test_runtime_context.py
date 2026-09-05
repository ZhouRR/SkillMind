"""Workspace、fixture Provider、ProductionRunContextBuilder の境界を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.agent.context_builder import (
    ContractStore,
    ProductionRunContextBuilder,
    create_fixture_tool_registry,
    create_run_tool_registry,
)
from projectmind.agent.domain import RunWorkspace
from projectmind.agent.fixture_providers import (
    CsvFixtureIssueProvider,
    GitFixtureRepositoryProvider,
)
from projectmind.agent.tool_gateway import RunToolContext, ToolProviderError
from projectmind.agent.workspace import WorkspaceManager
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.documents.source import ProjectDocumentContent
from projectmind.runs.domain import ClaimedRun

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


def _provider_context(capability: str, provider: str) -> RunToolContext:
    """Provider 単体契約検証用の Run identity を返す。"""

    contracts = ContractStore(CONTRACTS)
    registered = create_fixture_tool_registry(contracts).resolve(
        capability, provider=provider, integration_id=None
    )
    run_id = uuid4()
    root = Path("/tmp/projectmind-provider-tests") / str(run_id)
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

    provider = CsvFixtureIssueProvider(
        CONTRACTS / "fixtures" / "generic-read-providers" / "issues.csv"
    )
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

    root = CONTRACTS / "fixtures" / "generic-read-providers" / "repository"
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
        selected_sources={
            "primary-issues": {"capability": "issue.read/v1", "provider": "csv"}
        }
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
async def test_context_builder_rejects_uninstalled_production_source(tmp_path: Path) -> None:
    """Redmine/SVN 実装前に production source で dispatch されても fail closed とする。"""

    contracts = ContractStore(CONTRACTS)
    builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(contracts),
        model="claude-test",
    )
    manifest = _generic_manifest()
    manifest["capability_blueprint"]["resource_requirements"][0][
        "accepted_providers"
    ] = ["csv", "redmine"]
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
        "manifest_version": "projectmind/v1alpha1",
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
            "blueprint_version": "projectmind.capability-blueprint/v1",
            "identity": {
                "skill_key": "generic-repository-review",
                "source_hash": "sha256:" + ("a" * 64),
                "interpretation_id": "00000000-0000-4000-8000-000000000123",
                "interpreter_version": "projectmind-skill-interpreter/2.3.0",
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
    """非 JAF の published task から作られる通用 Run の claim を組み立てる。

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
    return ClaimedRun(
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


def _generic_builder(tmp_path: Path) -> ProductionRunContextBuilder:
    """通用 task 実行検証用の context builder を組み立てる。"""

    contracts = ContractStore(CONTRACTS)
    return ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_fixture_tool_registry(contracts),
        model="claude-test",
    )


@pytest.mark.asyncio
async def test_context_builder_resolves_generic_structured_source(tmp_path: Path) -> None:
    """非 JAF task と構造化 selected_sources から Tool と汎用 prompt を解決する。"""

    builder = _generic_builder(tmp_path)
    claimed = _generic_claimed(
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "csv"}}
    )
    context = await builder.build(claimed, sequence_start=1)

    assert [tool.capability for tool in context.tools] == ["issue.read/v1"]
    assert context.tools[0].provider == "csv"
    # 目標は Manifest が凍結した蓝图そのものから来る。JAF 固定文言は含まない。
    assert context.prompt.startswith(
        "Objective: Review the generic repository target and report findings."
    )
    assert "Analyze the JAF ticket" not in context.prompt
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
async def test_context_builder_opens_registered_workspace_write(tmp_path: Path) -> None:
    """SUPERVISED Run が明示宣言・許可した workspace.write/v1 を取得する (計画 §19 W2)。"""

    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": "workspace.write/v1", "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "SUPERVISED"
    }
    claimed = _generic_claimed(
        selected_sources={},
        required=False,
        allowed=("workspace.write/v1",),
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

    assert [tool.capability for tool in context.tools] == ["workspace.write/v1"]
    assert context.tools[0].provider == "workspace"


@pytest.mark.asyncio
async def test_context_builder_denies_workspace_search_below_profile_or_without_snapshot(
    tmp_path: Path,
) -> None:
    """Skill 宣言だけでは profile 上限や旧 permission snapshot を越えて検索権限を得られない。"""

    manifest = _generic_manifest(required=False)
    manifest["tools"] = [{"capability": "workspace.search/v1", "required": True}]
    manifest["capability_blueprint"]["execution_preferences"] = {
        "recommended_profile": "GUIDED"
    }
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
        allowed=("workspace.search/v1",),
        manifest=manifest,
    )
    guided.permission_snapshot_json["execution_profile"] = "GUIDED"
    with pytest.raises(LookupError, match="unavailable for the execution profile"):
        await builder.build(guided, sequence_start=1)

    historical = _generic_claimed(
        selected_sources={},
        required=False,
        allowed=("workspace.search/v1",),
        manifest=manifest,
    )
    with pytest.raises(ValueError, match="does not authorize workspace"):
        await builder.build(historical, sequence_start=1)


@pytest.mark.asyncio
async def test_context_builder_rejects_legacy_run_without_frozen_schema(tmp_path: Path) -> None:
    """Schema snapshot のない historical Run は読取専用とし再実行を拒否する。"""

    claimed = _generic_claimed(selected_sources={})
    claimed.task_snapshot_json.pop("input_schema_json")

    with pytest.raises(ValueError, match="missing frozen Schema"):
        await _generic_builder(tmp_path).build(claimed, sequence_start=1)


class _NoopDocumentSource:
    """Resolution 検証用の、内容を返さない ProjectDocumentSource。"""

    async def fetch(
        self, *, project_id: Any, folder: str, name: str
    ) -> ProjectDocumentContent | None:
        """解決段階では呼ばれないため常に None を返す。"""

        del project_id, folder, name
        return None


def _document_manifest() -> dict[str, Any]:
    """document.read/v1 を data source に要求する最小 published Manifest を作る。"""

    return {
        "manifest_version": "projectmind/v1alpha1",
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
            "blueprint_version": "projectmind.capability-blueprint/v1",
            "identity": {
                "skill_key": "project-document-review",
                "source_hash": "sha256:" + ("a" * 64),
                "interpretation_id": "00000000-0000-4000-8000-000000000123",
                "interpreter_version": "projectmind-skill-interpreter/2.3.0",
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


def _document_builder(tmp_path: Path) -> ProductionRunContextBuilder:
    """document.read/v1 を含む registry で通用 context builder を組み立てる。"""

    contracts = ContractStore(CONTRACTS)
    return ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager((tmp_path / "runs").resolve()),
        tool_registry=create_run_tool_registry(contracts, document_source=_NoopDocumentSource()),
        model="claude-test",
    )


@pytest.mark.asyncio
async def test_context_builder_resolves_project_document_source(tmp_path: Path) -> None:
    """document.read/v1 を選択した Run が project 文書 provider を解決する (離線解析層)。"""

    builder = _document_builder(tmp_path)
    claimed = _generic_claimed(
        manifest=_document_manifest(),
        allowed=("document.read/v1",),
        selected_sources={
            "project-doc": {"capability": "document.read/v1", "provider": "project"}
        },
    )
    context = await builder.build(claimed, sequence_start=1)

    assert [tool.capability for tool in context.tools] == ["document.read/v1"]
    assert context.tools[0].provider == "project"


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
async def test_context_builder_rejects_structured_provider_not_accepted(tmp_path: Path) -> None:
    """構造化 source の provider が accepted_providers 外なら fail closed とする。"""

    builder = _generic_builder(tmp_path)
    claimed = _generic_claimed(
        selected_sources={
            "primary-issues": {"capability": "issue.read/v1", "provider": "redmine"}
        }
    )
    with pytest.raises(ValueError, match="not accepted"):
        await builder.build(claimed, sequence_start=1)
