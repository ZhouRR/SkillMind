"""ClaimedRun snapshot を検証済み RunContext へ変換する。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.document_provider import DocumentProvider
from skillmind.agent.domain import (
    MaterializedResource,
    RegisteredTool,
    RunContext,
    RunLimits,
)
from skillmind.agent.repository_provider import RepositoryReadProvider
from skillmind.agent.repository_source import (
    RepositoryBindingRef,
    RepositorySnapshotSource,
)
from skillmind.agent.subagent import SUBAGENT_DISPATCH_CAPABILITY
from skillmind.agent.task_brief import (
    build_agent_task_brief,
    render_task_brief_prompt,
    resolve_execution_profile,
)
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolDefinition,
    ToolProvider,
    ToolProviderError,
    ToolRegistry,
)
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.agent.workspace_provider import (
    WorkspaceReadProvider,
    WorkspaceSearchProvider,
    WorkspaceWriteProvider,
)
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.snapshot import (
    DOCUMENT_PROVIDER,
    DOCUMENT_READ_CAPABILITY,
    selected_document_snapshots,
)
from skillmind.documents.source import ProjectDocumentSource
from skillmind.effects.proposal import CHANGE_PROPOSE_CAPABILITY
from skillmind.integrations.domain import ResourceBindingLevel, binding_checksum
from skillmind.runs.domain import ClaimedRun
from skillmind.runs.interaction import INTERACTION_REQUEST_CAPABILITY
from skillmind.skills.capability_blueprint import resolve_capability_blueprint
from skillmind.skills.resource_binding import is_deferred_execution_capability


class ContractStore:
    """Configured contracts root 下の JSON object だけを読み込む。"""

    def __init__(self, root: Path) -> None:
        """Existence を検証し、contract path 解決の root を固定する。"""

        self._root = root.resolve(strict=True)

    @property
    def root(self) -> Path:
        """Tool 契約を解決する read-only root を返す。"""

        return self._root

    def load(self, relative_path: str) -> dict[str, Any]:
        """Traversal と non-object JSON を拒否し、契約の defensive copy を返す。"""

        path = (self._root / relative_path).resolve(strict=True)
        if not path.is_relative_to(self._root) or path.is_symlink():
            raise ValueError("Contract path escapes the configured root")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Contract JSON must be an object")
        return value


def _read_tool_definitions(
    contracts: ContractStore,
    *,
    redmine_issue_provider: ToolProvider | None = None,
    database_provider: ToolProvider | None = None,
    mcp_provider: ToolProvider | None = None,
    repository_source: RepositorySnapshotSource | None = None,
) -> tuple[ToolDefinition, ...]:
    """注入済みの実 Provider だけを公開し、未設定の資源を合成 data で補わない。"""

    definitions: list[ToolDefinition] = []
    if mcp_provider is not None:
        definitions.append(ToolDefinition(
            capability="mcp.read/v1",
            description="Read text or Base64 content from one allowed MCP resource URI",
            request_schema=contracts.load("tools/mcp.read/v1/request.schema.json"),
            response_schema=contracts.load("tools/mcp.read/v1/response.schema.json"),
            error_schema=contracts.load("tools/mcp.read/v1/error.schema.json"),
            providers={"mcp": mcp_provider},
        ))
    if database_provider is not None:
        definitions.append(
            ToolDefinition(
                capability="database.read/v1",
                description=(
                    "Read allowed PostgreSQL tables with columns, equality filters "
                    "and bounded rows; no SQL input"
                ),
                request_schema=contracts.load("tools/database.read/v1/request.schema.json"),
                response_schema=contracts.load("tools/database.read/v1/response.schema.json"),
                error_schema=contracts.load("tools/database.read/v1/error.schema.json"),
                providers={"postgres": database_provider},
            )
        )
    if redmine_issue_provider is not None:
        definitions.append(ToolDefinition(
            capability="issue.read/v1",
            description="Read one issue from the bound Integration",
            request_schema=contracts.load("tools/issue.read/v1/request.schema.json"),
            response_schema=contracts.load("tools/issue.read/v1/response.schema.json"),
            error_schema=contracts.load("tools/issue.read/v1/error.schema.json"),
            providers={"redmine": redmine_issue_provider},
        ))
    if repository_source is not None:
        bound = RepositoryReadProvider(repository_source)
        definitions.append(ToolDefinition(
            capability="repository.read/v1",
            description="Read one UTF-8 file from the bound repository at a fixed revision",
            request_schema=contracts.load("tools/repository.read/v1/request.schema.json"),
            response_schema=contracts.load("tools/repository.read/v1/response.schema.json"),
            error_schema=contracts.load("tools/repository.read/v1/error.schema.json"),
            providers={"git": bound, "svn": bound},
        ))
    return tuple(definitions)


def document_read_tool_definition(
    contracts: ContractStore, source: ProjectDocumentSource
) -> ToolDefinition:
    """Project 文書を読む document.read/v1 の Tool 定義を組み立てる。"""

    return ToolDefinition(
        capability="document.read/v1",
        description="Read one UTF-8 project document at a fixed content hash",
        request_schema=contracts.load("tools/document.read/v1/request.schema.json"),
        response_schema=contracts.load("tools/document.read/v1/response.schema.json"),
        error_schema=contracts.load("tools/document.read/v1/error.schema.json"),
        providers={DOCUMENT_PROVIDER: DocumentProvider(source)},
    )


def create_run_tool_registry(
    contracts: ContractStore,
    *,
    document_source: ProjectDocumentSource,
    redmine_issue_provider: ToolProvider | None = None,
    database_provider: ToolProvider | None = None,
    mcp_provider: ToolProvider | None = None,
    repository_source: RepositorySnapshotSource | None = None,
    subagent_provider: ToolProvider | None = None,
    deferred_features_enabled: bool = True,
) -> ToolRegistry:
    """Project 文書、実 Integration と platform 能力を registry へ登録する。"""

    return ToolRegistry(
        (
            *_read_tool_definitions(
                contracts,
                redmine_issue_provider=redmine_issue_provider,
                database_provider=database_provider,
                mcp_provider=mcp_provider,
                repository_source=repository_source,
            ),
            document_read_tool_definition(contracts, document_source),
            *_workspace_tool_definitions(contracts),
            _interaction_tool_definition(contracts),
            *((_change_propose_tool_definition(contracts),) if deferred_features_enabled else ()),
            *(_subagent_tool_definitions(contracts, subagent_provider)
              if deferred_features_enabled else ()),
        )
    )


class DeferredInteractionProvider:
    """PreToolUse defer が破られた場合に fail closed する control Provider。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Interaction は Worker transaction だけが保存できるため直接実行を拒否する。"""

        del context, arguments
        raise ToolProviderError(
            "unavailable",
            "Interaction request must be deferred by Skillmind",
            retryable=False,
        )


class DeferredChangeProposalProvider:
    """Proposal control Tool が Provider 経由で effect を起こさないよう fail closed にする。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Proposal は Worker transaction だけが保存できるため直接実行を拒否する。"""

        del context, arguments
        raise ToolProviderError(
            "unavailable",
            "ChangeProposal request must be deferred by Skillmind",
            retryable=False,
        )


def _subagent_tool_definitions(
    contracts: ContractStore, provider: ToolProvider | None
) -> tuple[ToolDefinition, ...]:
    """扇出 control Tool を登録する。engine を持たない環境 (API 側) では登録しない。

    Provider が無いのに Tool だけ出すと、Agent が呼べる面はあるのに必ず失敗する状態になる。
    実行できない能力は最初から見せない。
    """

    if provider is None:
        return ()
    return (
        ToolDefinition(
            capability=SUBAGENT_DISPATCH_CAPABILITY,
            description=(
                "Fan out bounded read-only sub-analyses of independent aspects and collect "
                "their conclusions; sub-agents cannot write, ask, propose, or fan out again"
            ),
            request_schema=contracts.load("tools/subagent.dispatch/v1/request.schema.json"),
            response_schema=contracts.load("tools/subagent.dispatch/v1/response.schema.json"),
            error_schema=contracts.load("tools/subagent.dispatch/v1/error.schema.json"),
            providers={"platform": provider},
            unbound_provider="platform",
            minimum_execution_profile="SUPERVISED",
        ),
    )


def _interaction_tool_definition(contracts: ContractStore) -> ToolDefinition:
    """ユーザー入力前に SDK を停止する platform control Tool を登録する。"""

    return ToolDefinition(
        capability=INTERACTION_REQUEST_CAPABILITY,
        description=(
            "Pause this run and request structured user input when a required fact, choice, "
            "review, or approval cannot be decided safely"
        ),
        request_schema=contracts.load("tools/interaction.request/v1/request.schema.json"),
        response_schema=contracts.load("tools/interaction.request/v1/response.schema.json"),
        error_schema=contracts.load("tools/interaction.request/v1/error.schema.json"),
        providers={"platform": DeferredInteractionProvider()},
        unbound_provider="platform",
        minimum_execution_profile="GUIDED",
        defer_execution=True,
    )


def _change_propose_tool_definition(contracts: ContractStore) -> ToolDefinition:
    """Agent の提案を外部 write から切り離して Worker へ defer する control Tool。"""

    return ToolDefinition(
        capability=CHANGE_PROPOSE_CAPABILITY,
        description=(
            "Create a structured external change proposal; this never applies the change and "
            "Skillmind independently validates approval and scope"
        ),
        request_schema=contracts.load("tools/change.propose/v1/request.schema.json"),
        response_schema=contracts.load("tools/change.propose/v1/response.schema.json"),
        error_schema=contracts.load("tools/change.propose/v1/error.schema.json"),
        providers={"platform": DeferredChangeProposalProvider()},
        unbound_provider="platform",
        minimum_execution_profile="GUIDED",
        defer_execution=True,
    )


def _workspace_tool_definitions(contracts: ContractStore) -> tuple[ToolDefinition, ...]:
    """Run 内の読取と制限付き書込を精確な capability version ごとに登録する。"""

    return (
        ToolDefinition(
            capability="workspace.read/v1",
            description="Read one UTF-8 file from the isolated Run workspace",
            request_schema=contracts.load("tools/workspace.read/v1/request.schema.json"),
            response_schema=contracts.load("tools/workspace.read/v1/response.schema.json"),
            error_schema=contracts.load("tools/workspace.read/v1/error.schema.json"),
            providers={"workspace": WorkspaceReadProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="GUIDED",
        ),
        ToolDefinition(
            capability="workspace.search/v1",
            description="Search UTF-8 files in the isolated Run workspace",
            request_schema=contracts.load("tools/workspace.search/v1/request.schema.json"),
            response_schema=contracts.load("tools/workspace.search/v1/response.schema.json"),
            error_schema=contracts.load("tools/workspace.search/v1/error.schema.json"),
            providers={"workspace": WorkspaceSearchProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
        ToolDefinition(
            capability="workspace.write/v1",
            description="Write one UTF-8 file to the isolated Run workspace or output",
            request_schema=contracts.load("tools/workspace.write/v1/request.schema.json"),
            response_schema=contracts.load("tools/workspace.write/v1/response.schema.json"),
            error_schema=contracts.load("tools/workspace.write/v1/error.schema.json"),
            providers={"workspace": WorkspaceWriteProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
        ToolDefinition(
            capability="workspace.write/v2",
            description=(
                "Write one UTF-8 file within the isolated Run; output files receive an "
                "immutable Artifact reference only after their exact bytes are committed"
            ),
            request_schema=contracts.load("tools/workspace.write/v2/request.schema.json"),
            response_schema=contracts.load("tools/workspace.write/v2/response.schema.json"),
            error_schema=contracts.load("tools/workspace.write/v2/error.schema.json"),
            providers={"workspace": WorkspaceWriteProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
    )


class ProductionRunContextBuilder:
    """Immutable Run snapshot と local runtime dependency から Agent context を構築する。"""

    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager,
        tool_registry: ToolRegistry,
        model: str | None,
        materializer: WorkspaceMaterializer | None = None,
        deferred_features_enabled: bool = True,
    ) -> None:
        """Workspace、Tool と model の Worker 起動時 snapshot を保持する。

        ``materializer`` を渡すと Run 準備段階で冻结資源を input/ へ只読物化する (計画 §19 W3)。
        None のときは物化を行わない (offline/未配線環境の従来挙動)。
        """

        self._workspace_manager = workspace_manager
        self._tool_registry = tool_registry
        self._model = model
        self._materializer = materializer
        self._deferred_features_enabled = deferred_features_enabled

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """Input/Schema/Source/Permission を再検証し、最小 Tool 付き context を返す。"""

        if self._model is None:
            # 実行時 model を CLI default へ暗黙委任すると Run の監査 snapshot が不完全になる。
            raise ValueError("ANTHROPIC_MODEL must be configured before dispatch")
        task = claimed_run.task_snapshot_json
        _required_string(task, "input_schema")
        _required_string(task, "output_schema")
        input_schema = _snapshot_schema(task, key="input_schema_json")
        output_schema = _snapshot_schema(task, key="output_schema_json")
        _validate_json(input_schema, claimed_run.input_json, label="Run input")
        permission = claimed_run.permission_snapshot_json
        _validate_actor(permission, claimed_run.actor_id)
        if permission.get("mode") != "auto_read_only":
            raise ValueError("M0 runtime requires auto_read_only permission mode")
        allowed = permission.get("allowed_capabilities")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("Permission snapshot contains invalid capabilities")
        # 旧権限を削って別 Run として実行せず、物化・モデル起動の前に拒否する。
        if not self._deferred_features_enabled and any(
            is_deferred_execution_capability(capability)
            for capability in allowed
        ):
            raise ValueError("Run snapshot requires disabled execution features")

        # 不変 SkillVersion snapshot を先に確定してから、その Manifest を根拠に Tool を解決する。
        # 業務固有 task に依存せず、任意 published task の Run を同一経路で実行する。
        manifest = _verified_manifest(claimed_run, task)
        blueprint = resolve_capability_blueprint(manifest)
        if blueprint is None:
            raise ValueError("Run SkillVersion Manifest does not declare a CapabilityBlueprint")
        execution_profile = resolve_execution_profile(blueprint).profile.value
        snapshot_profile = permission.get("execution_profile")
        declares_workspace = any(
            isinstance(item, Mapping)
            and isinstance(item.get("capability"), str)
            and str(item["capability"]).startswith("workspace.")
            for item in _sequence(manifest.get("tools"))
        )
        if snapshot_profile is None and declares_workspace:
            # 旧 snapshot に後付けで workspace 権限を生やさず、新規 Run だけが profile を固定する。
            raise ValueError("Permission snapshot does not authorize workspace capabilities")
        if snapshot_profile is not None and snapshot_profile != execution_profile:
            raise ValueError("Permission snapshot execution profile does not match the Skill")
        tools, repository_bindings = _resolve_source_tools(
            claimed_run,
            manifest,
            self._tool_registry,
            allowed,
            execution_profile=execution_profile,
        )
        document_snapshots = selected_document_snapshots(
            claimed_run.selected_sources_json, project_id=claimed_run.project_id
        )

        snapshot = claimed_run.limits_snapshot_json
        limits = RunLimits(
            max_turns=_positive_int(snapshot, "max_turns"),
            wall_timeout_seconds=_positive_int(snapshot, "wall_timeout_seconds"),
            max_output_bytes=_positive_int(snapshot, "max_output_bytes"),
            max_budget_usd=_optional_positive_number(snapshot, "max_budget_usd"),
        )
        # Workspace を先に確定し、Agent 起動前に冻结資源を input/ へ只読物化する (計画 §19 W3)。
        # 物化は permission/Tool 解決の後に置き、許可されていない資源を先に落とさない。
        workspace = self._workspace_manager.initialize(claimed_run.run_id)
        materialized: tuple[MaterializedResource, ...] = ()
        if self._materializer is not None:
            prepared_input = await self._materializer.materialize(
                claimed_run=claimed_run,
                workspace=workspace,
                project_id=claimed_run.project_id,
                run_id=claimed_run.run_id,
                blueprint=blueprint,
                repository_bindings=repository_bindings,
                document_snapshots=document_snapshots,
            )
            workspace = prepared_input.workspace
            materialized = prepared_input.resources
        # Skill guidance は Brief を経由してのみ Agent へ届く。permission と Tool 解決を先に
        # 確定させてから組み立て、Brief が許可されていない能力を語らないようにする。
        brief = build_agent_task_brief(
            run_id=claimed_run.run_id,
            task_snapshot=task,
            manifest=manifest,
            selected_sources=claimed_run.selected_sources_json,
            tools=tools,
            limits=limits,
            segment_no=claimed_run.segment_no,
            segment_objective=_segment_objective(claimed_run.segment_objective_json),
            checkpoint=claimed_run.checkpoint_json,
            # 物化器が実際に書いた落点だけを Brief へ載せる (計画 §19 W6)。未配線環境で存在
            # しない directory を案内すると、Agent は読めない path を試して行き詰まる。
            materialized=materialized,
        )
        return RunContext(
            run_id=claimed_run.run_id,
            run_attempt_id=claimed_run.run_attempt_id,
            project_id=claimed_run.project_id,
            user_id=claimed_run.actor_id,
            prompt=render_task_brief_prompt(
                brief.brief,
                input_json=claimed_run.input_json,
                output_schema=output_schema,
            ),
            task_snapshot=dict(task),
            skill_snapshots=(manifest,),
            resolved_sources=dict(claimed_run.selected_sources_json),
            permission_snapshot=dict(permission),
            workspace=workspace,
            limits=limits,
            result_schema=output_schema,
            tools=tuple(tools),
            model=self._model,
            sequence_start=sequence_start,
            trace={
                "attempt_no": claimed_run.attempt_no,
                "segment_no": claimed_run.segment_no,
                "continuation_mode": claimed_run.continuation_mode.value,
            },
            task_brief=brief.brief,
            task_brief_checksum=brief.checksum,
        )


def _segment_objective(value: Mapping[str, Any] | None) -> str | None:
    """Segment row の公開 objective text だけを Brief override として返す。"""

    if value is None:
        return None
    objective = value.get("text")
    return objective if isinstance(objective, str) and objective else None


def _snapshot_schema(task: Mapping[str, Any], *, key: str) -> dict[str, Any]:
    """Run に固定された generated Schema だけを読み、legacy Run の再実行を拒否する。"""

    frozen = task.get(key)
    if isinstance(frozen, dict):
        schema = dict(frozen)
        Draft202012Validator.check_schema(schema)
        return schema
    raise ValueError(f"Run task snapshot is missing frozen Schema: {key}")


def _validate_json(schema: Mapping[str, Any], value: Any, *, label: str) -> None:
    """Public 値を error へ含めず、Draft 2020-12 契約への不一致を拒否する。"""

    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        raise ValueError(f"{label} did not match its contract")


def _verified_manifest(claimed_run: ClaimedRun, task: Mapping[str, Any]) -> Mapping[str, Any]:
    """唯一の SkillVersion snapshot から不変 Manifest を取り出し checksum 同一性を検証する。

    Manifest checksum と task 側の記録が一致しなければ、凍結された published 内容から
    ずれているとみなして fail closed とする。実行に使う Manifest 契約の唯一の入口。
    """

    if len(claimed_run.skill_snapshots_json) != 1:
        raise ValueError("Run must bind exactly one SkillVersion snapshot")
    skill_snapshot = claimed_run.skill_snapshots_json[0]
    manifest = skill_snapshot.get("manifest")
    checksum = skill_snapshot.get("manifest_checksum")
    if not isinstance(manifest, dict) or not isinstance(checksum, str):
        raise ValueError("Run SkillVersion snapshot is incomplete")
    actual_checksum = f"sha256:{sha256_hex(canonical_json(manifest))}"
    if checksum != actual_checksum or task.get("manifest_checksum") != checksum:
        raise ValueError("Run SkillVersion Manifest checksum does not match frozen content")
    if task.get("skill_version_id") != skill_snapshot.get("skill_version_id"):
        raise ValueError("Task and SkillVersion snapshot identity do not match")
    return manifest


def _resolve_source_tools(
    claimed_run: ClaimedRun,
    manifest: Mapping[str, Any],
    registry: ToolRegistry,
    allowed: Sequence[str],
    *,
    execution_profile: str,
) -> tuple[list[RegisteredTool], dict[str, RepositoryBindingRef]]:
    """Blueprint の resource_requirements と選択済み source から最小 Tool 集合を解決する。

    業務固有 capability には依存せず、blueprint の宣言順に要求を走査する。必須要求が未選択
    なら fail closed とし、解決した capability は必ず permission snapshot の
    allowed_capabilities に含まれていなければならない (Interpreter/Manifest が権限を拡大
    できない不変条件を Runtime 側でも守る)。

    併せて repository 種別の凍結 binding を requirement_key ごとに返す。物化 (計画 §19 W4) は
    この検証済み結果だけを入力とし、Run snapshot を再解釈しない。
    """

    tools: list[RegisteredTool] = []
    repository_bindings: dict[str, RepositoryBindingRef] = {}
    blueprint = manifest.get("capability_blueprint")
    requirements = (
        _sequence(blueprint.get("resource_requirements")) if isinstance(blueprint, Mapping) else []
    )
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        selected_source = _selected_source(requirement, claimed_run)
        if selected_source is None:
            if bool(requirement.get("required", False)):
                raise ValueError("Required data source has no selected provider")
            continue
        capability, provider, integration_id, binding_id = selected_source
        if capability not in allowed:
            raise ValueError("Resolved Tool is not allowed by the permission snapshot")
        requirement_key = requirement.get("key")
        if (
            requirement.get("kind") == "repository"
            and isinstance(requirement_key, str)
            and integration_id is not None
            and binding_id is not None
        ):
            repository_bindings[requirement_key] = RepositoryBindingRef(
                provider=provider,
                integration_id=integration_id,
                binding_id=binding_id,
            )
        resolved = registry.resolve(
            capability,
            provider=provider,
            integration_id=integration_id,
            binding_id=binding_id,
            execution_profile=execution_profile,
        )
        if capability == DOCUMENT_READ_CAPABILITY and any(
            tool.capability == capability for tool in tools
        ):
            # 複数の文書 slot は同じ Run 集合を読む。SDK 名を重複登録しない。
            continue
        tools.append(resolved)
    resolved_capabilities = {tool.capability for tool in tools}
    for tool_requirement in _sequence(manifest.get("tools")):
        if not isinstance(tool_requirement, Mapping):
            continue
        tool_capability = tool_requirement.get("capability")
        if not isinstance(tool_capability, str) or tool_capability in resolved_capabilities:
            continue
        if tool_capability not in allowed:
            if bool(tool_requirement.get("required", False)):
                raise ValueError("Resolved Tool is not allowed by the permission snapshot")
            continue
        try:
            resolved = registry.resolve_unbound(
                tool_capability,
                execution_profile=execution_profile,
            )
        except LookupError:
            if bool(tool_requirement.get("required", False)):
                raise
            continue
        tools.append(resolved)
        resolved_capabilities.add(tool_capability)
    if (
        INTERACTION_REQUEST_CAPABILITY in allowed
        and INTERACTION_REQUEST_CAPABILITY not in resolved_capabilities
    ):
        tools.append(
            registry.resolve_unbound(
                INTERACTION_REQUEST_CAPABILITY,
                execution_profile=execution_profile,
            )
        )
    if (
        CHANGE_PROPOSE_CAPABILITY in allowed
        and CHANGE_PROPOSE_CAPABILITY not in resolved_capabilities
    ):
        tools.append(
            registry.resolve_unbound(
                CHANGE_PROPOSE_CAPABILITY,
                execution_profile=execution_profile,
            )
        )
    return tools, repository_bindings


def _selected_source(
    requirement: Mapping[str, Any], claimed_run: ClaimedRun
) -> tuple[str, str, UUID | None, UUID | None] | None:
    """構造化 snapshot を再検証し、capability・provider と frozen Integration identity を返す。

    要求宣言は blueprint へ一本化したため、照合 key は requirement の `key` そのものとする。
    以前は capability 一致で走査していたが、同 capability を共有する要求同士で取り違える
    余地があり、宣言側と選択側で key がずれると解決不能になっていた。
    """

    requirement_key = requirement.get("key")
    if not isinstance(requirement_key, str):
        return None
    value = claimed_run.selected_sources_json.get(requirement_key)
    if not isinstance(value, Mapping):
        return None
    capability = value.get("capability")
    if not isinstance(capability, str):
        raise ValueError("Selected data source capability is invalid")
    declared = frozenset(
        item for item in _sequence(requirement.get("capabilities")) if isinstance(item, str)
    )
    if capability not in declared:
        # 凍結 snapshot が要求宣言外の capability を指すのは改竄か不整合。fail closed。
        raise ValueError("Selected capability is not declared by the resource requirement")
    provider = value.get("provider")
    if not isinstance(provider, str):
        raise ValueError("Selected data source provider is invalid")
    if requirement.get("kind") == "document":
        if (
            capability != DOCUMENT_READ_CAPABILITY
            or provider != DOCUMENT_PROVIDER
            or requirement.get("access", "read") != "read"
        ):
            raise ValueError("Selected document source is invalid")
        selected_document_snapshots({requirement_key: value}, project_id=claimed_run.project_id)
        return capability, provider, None, None
    raw_integration_id = value.get("integration_id")
    if raw_integration_id is None:
        raise ValueError("Selected data source requires a frozen Integration binding")
    try:
        integration_id = UUID(str(raw_integration_id))
    except ValueError as error:
        raise ValueError("Selected Integration identity is invalid") from error
    binding_id = _validate_run_binding_snapshot(
        claimed_run,
        requirement_key=requirement_key,
        value=value,
        integration_id=integration_id,
        provider=provider,
    )
    return capability, provider, integration_id, binding_id


def _validate_run_binding_snapshot(
    claimed_run: ClaimedRun,
    *,
    requirement_key: str,
    value: Mapping[str, Any],
    integration_id: UUID,
    provider: str,
) -> UUID:
    """Run source の permission-sensitive fields と checksum の一致を検証する。"""

    revision = value.get("revision")
    scope = value.get("scope")
    resource_kind = value.get("resource_kind")
    binding_capability = value.get("binding_capability")
    checksum = value.get("binding_checksum")
    binding_id = value.get("binding_id")
    if (
        not isinstance(revision, str)
        or not isinstance(scope, dict)
        or not isinstance(resource_kind, str)
        or not isinstance(binding_capability, str)
        or not isinstance(checksum, str)
        or not isinstance(binding_id, str)
    ):
        raise ValueError("Run ResourceBinding snapshot is incomplete")
    expected = binding_checksum(
        project_id=claimed_run.project_id,
        scope_level=ResourceBindingLevel.RUN,
        scope_key=str(claimed_run.run_id),
        requirement_key=requirement_key,
        resource_kind=resource_kind,
        integration_id=integration_id,
        provider=provider,
        capability_version=binding_capability,
        revision=revision,
        scope=scope,
    )
    if checksum != expected:
        raise ValueError("Run ResourceBinding checksum does not match frozen content")
    try:
        return UUID(binding_id)
    except ValueError as error:
        raise ValueError("Run ResourceBinding identity is invalid") from error


def _sequence(value: Any) -> Sequence[Any]:
    """List 状の値だけを列挙対象として返し、str/bytes を除外する。"""

    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()


def _validate_actor(permission: Mapping[str, Any], actor_id: UUID) -> None:
    """Permission snapshot に固定した actor と claim identity の一致を保証する。"""

    try:
        snapshot_actor = UUID(str(permission.get("actor_id")))
    except ValueError as error:
        raise ValueError("Permission snapshot actor is invalid") from error
    if snapshot_actor != actor_id:
        raise ValueError("Permission snapshot actor does not match the claimed Run")


def _required_string(value: Mapping[str, Any], key: str) -> str:
    """Task snapshot の必須 string 参照を返す。"""

    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Task snapshot is missing {key}")
    return result


def _positive_int(value: Mapping[str, Any], key: str) -> int:
    """Resource limit の bool や無制限値を拒否する。"""

    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ValueError(f"Run limit is invalid: {key}")
    return result


def _optional_positive_number(value: Mapping[str, Any], key: str) -> float | None:
    """Optional budget を正の finite float へ制限する。"""

    result = value.get(key)
    if result is None:
        return None
    if isinstance(result, bool) or not isinstance(result, int | float):
        raise ValueError(f"Run limit is invalid: {key}")
    number = float(result)
    if number <= 0 or number == float("inf") or number != number:
        raise ValueError(f"Run limit is invalid: {key}")
    return number
