"""Provider と versioned 契約を同じ capability から装配する唯一の Tool catalog。"""

from __future__ import annotations

from collections.abc import Mapping

from skillmind.agent.audit_export import AuditExportProvider
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.control_providers import (
    DeferredChangeProposalProvider,
    DeferredInteractionProvider,
    UnavailableDocumentReadiness,
)
from skillmind.agent.document_inspection import DocumentInspectProvider
from skillmind.agent.document_listing import DocumentListProvider
from skillmind.agent.document_provider import DocumentConvertProvider, DocumentProvider
from skillmind.agent.json_schema_provider import JsonSchemaValidateProvider
from skillmind.agent.repository_provider import RepositoryReadProvider
from skillmind.agent.repository_source import (
    RepositorySnapshotSource,
)
from skillmind.agent.subagent import SUBAGENT_DISPATCH_CAPABILITY
from skillmind.agent.tool_gateway import (
    ToolDefinition,
    ToolProvider,
    ToolRegistry,
)
from skillmind.agent.tool_sequence import ToolSequenceProvider
from skillmind.agent.workspace_provider import (
    WorkspaceReadProvider,
    WorkspaceSearchProvider,
    WorkspaceWriteProvider,
)
from skillmind.documents.observation_repository import DocumentObservationLookup
from skillmind.documents.snapshot import (
    DOCUMENT_CONVERT_CAPABILITY,
    DOCUMENT_INSPECT_CAPABILITY,
    DOCUMENT_LIST_CAPABILITY,
    DOCUMENT_PROVIDER,
)
from skillmind.documents.source import ProjectDocumentSource
from skillmind.effects.proposal import CHANGE_PROPOSE_CAPABILITY
from skillmind.runs.interaction import INTERACTION_REQUEST_CAPABILITY
from skillmind.skills.document_prerequisites import (
    DOCUMENT_READINESS_CAPABILITY,
)


def _tool_definition(
    contracts: ContractStore,
    *,
    capability: str,
    description: str,
    providers: Mapping[str, ToolProvider],
    unbound_provider: str | None = None,
    minimum_execution_profile: str = "GUIDED",
    defer_execution: bool = False,
    sequence_safe: bool = False,
) -> ToolDefinition:
    """三つの Schema を同じ capability/version から取得し、登録情報のずれを防ぐ。"""

    return ToolDefinition(
        capability=capability,
        description=description,
        request_schema=contracts.load(f"tools/{capability}/request.schema.json"),
        response_schema=contracts.load(f"tools/{capability}/response.schema.json"),
        error_schema=contracts.load(f"tools/{capability}/error.schema.json"),
        providers=providers,
        unbound_provider=unbound_provider,
        minimum_execution_profile=minimum_execution_profile,
        defer_execution=defer_execution,
        sequence_safe=sequence_safe and not defer_execution,
    )


def _read_tool_definitions(
    contracts: ContractStore,
    *,
    redmine_issue_provider: ToolProvider | None = None,
    database_provider: ToolProvider | None = None,
    mcp_provider: ToolProvider | None = None,
    mcp_tools_provider: ToolProvider | None = None,
    mcp_query_provider: ToolProvider | None = None,
    repository_source: RepositorySnapshotSource | None = None,
) -> tuple[ToolDefinition, ...]:
    """注入済みの実 Provider だけを公開し、未設定の資源を合成 data で補わない。"""

    definitions: list[ToolDefinition] = []
    for capability, provider, description in (
        (
            "mcp.tools/v1",
            mcp_tools_provider,
            "Discover frozen MCP schemas, server version and connection references. "
            "Use reserve_desktop=true before planning desktop execution "
            "to obtain a Run reservation. "
            "This only excludes other SKM Runs at the same endpoint; "
            "external exclusivity is not verified. "
            "Does not start an app or provide unobserved environment/build configuration. "
            + str(contracts.load("tools/mcp.call/v1/request.schema.json")["description"]),
        ),
        (
            "mcp.query/v1",
            mcp_query_provider,
            "Call tools explicitly authorized as read in the frozen catalog. Other tools require "
            "change.propose approval; never call them here.",
        ),
    ):
        if provider is not None:
            definitions.append(
                _tool_definition(contracts,
                    sequence_safe=True,
                    capability=capability,
                    description=description,
                    providers={"mcp": provider},
                )
            )
    if mcp_provider is not None:
        definitions.append(
            _tool_definition(contracts,
                sequence_safe=True,
                capability="mcp.read/v1",
                description="Read text or Base64 content from one allowed MCP resource URI",
                providers={"mcp": mcp_provider},
            )
        )
    if database_provider is not None:
        definitions.append(
            _tool_definition(contracts,
                sequence_safe=True,
                capability="database.read/v1",
                description=(
                    "Read allowed PostgreSQL tables with columns, equality filters "
                    "and bounded rows; use include_schema to inspect columns and primary keys "
                    "even for empty tables before proposing a write; no SQL input"
                ),
                providers={"postgres": database_provider},
            )
        )
    if database_provider is not None:
        for capability, description in (
            (
                "database.read/v2",
                "Read the exact authorized PostgreSQL table; no SQL. Use "
                "database.describe/v1 when columns or keys are unknown. Preserve query "
                "meaning. After one correctable failure, link a corrected read with "
                "recovery.failed_tool_call_id and schema_evidence_ref. Do not repeat "
                "unchanged queries.",
            ),
            (
                "database.describe/v1",
                "Inspect only the named authorized table's columns and ordered primary key, "
                "without reading rows. Reuse Run observations unless refresh=true or "
                "correcting a structural error. recovery_from links the original failed "
                "ToolCall. Cached structure is not current DDL or write permission.",
            ),
        ):
            definitions.append(
                _tool_definition(contracts,
                    sequence_safe=True,
                    capability=capability,
                    description=description,
                    providers={"postgres": database_provider},
                )
            )
    if redmine_issue_provider is not None:
        definitions.append(
            _tool_definition(contracts,
                sequence_safe=True,
                capability="issue.read/v1",
                description="Read one issue from the bound Integration",
                providers={"redmine": redmine_issue_provider},
            )
        )
    if repository_source is not None:
        bound = RepositoryReadProvider(repository_source)
        definitions.append(
            _tool_definition(contracts,
                sequence_safe=True,
                capability="repository.read/v1",
                description="Read one UTF-8 file from the bound repository at a fixed revision",
                providers={"git": bound, "svn": bound},
            )
        )
    return tuple(definitions)


def document_read_tool_definition(
    contracts: ContractStore, source: ProjectDocumentSource
) -> ToolDefinition:
    """Project 文書を読む document.read/v1 の Tool 定義を組み立てる。"""

    return _tool_definition(contracts,
        sequence_safe=True,
        capability="document.read/v1",
        description="Read one UTF-8 project document at a fixed content hash",
        providers={DOCUMENT_PROVIDER: DocumentProvider(source)},
    )


def document_convert_tool_definition(
    contracts: ContractStore, source: ProjectDocumentSource,
    *, observations: DocumentObservationLookup | None = None,
) -> ToolDefinition:
    """凍結 Excel を Worker 内で明示変換する Tool を登録する。"""

    return _tool_definition(contracts,
        sequence_safe=True,
        capability=DOCUMENT_CONVERT_CAPABILITY,
        description=(
            "Convert one frozen Excel using Worker MarkItDown. Set publish_artifact=true to "
            "save the exact Markdown as a Run Artifact for approved document backup; use its "
            "artifact_refs and artifact size/hash instead of rewriting the Markdown."
        ),
        providers={DOCUMENT_PROVIDER: DocumentConvertProvider(source, observations=observations)},
    )


def document_inspect_tool_definition(
    contracts: ContractStore, source: ProjectDocumentSource
) -> ToolDefinition:
    """凍結範囲の実 storage metadata だけを観測する Tool を登録する。"""

    return _tool_definition(contracts,
        sequence_safe=True,
        capability=DOCUMENT_INSPECT_CAPABILITY,
        description=(
            "Inspect storage LastModified, Version ID and ETag of one frozen document "
            "without downloading its bytes"
        ),
        providers={DOCUMENT_PROVIDER: DocumentInspectProvider(source)},
    )


def document_list_tool_definition(
    contracts: ContractStore, source: ProjectDocumentSource
) -> ToolDefinition:
    """凍結集合の directory 分頁・実 metadata 条件を明示能力として登録する。"""

    return _tool_definition(contracts,
        sequence_safe=True,
        capability=DOCUMENT_LIST_CAPABILITY,
        description=(
            "Page through frozen authorized documents by directory and storage LastModified; "
            "follow every next_cursor and convert matches using each entry's observation Evidence"
        ),
        providers={DOCUMENT_PROVIDER: DocumentListProvider(source)},
    )


def create_run_tool_registry(
    contracts: ContractStore,
    *,
    document_source: ProjectDocumentSource,
    document_observations: DocumentObservationLookup | None = None,
    document_readiness_provider: ToolProvider | None = None,
    audit_export_provider: ToolProvider | None = None,
    redmine_issue_provider: ToolProvider | None = None,
    database_provider: ToolProvider | None = None,
    mcp_provider: ToolProvider | None = None,
    mcp_tools_provider: ToolProvider | None = None,
    mcp_query_provider: ToolProvider | None = None,
    repository_source: RepositorySnapshotSource | None = None,
    subagent_provider: ToolProvider | None = None,
    deferred_features_enabled: bool = True,
    database_writes_enabled: bool = False,
    document_writes_enabled: bool = False,
    git_writes_enabled: bool = False,
    mcp_tools_enabled: bool = False,
) -> ToolRegistry:
    """Project 文書、実 Integration と platform 能力を registry へ登録する。"""

    return ToolRegistry(
        (
            *_read_tool_definitions(
                contracts,
                redmine_issue_provider=redmine_issue_provider,
                database_provider=database_provider,
                mcp_provider=mcp_provider,
                mcp_tools_provider=mcp_tools_provider,
                mcp_query_provider=mcp_query_provider,
                repository_source=repository_source,
            ),
            document_read_tool_definition(contracts, document_source),
            document_convert_tool_definition(
                contracts, document_source, observations=document_observations
            ),
            document_inspect_tool_definition(contracts, document_source),
            document_list_tool_definition(contracts, document_source),
            _tool_definition(contracts,
                sequence_safe=True,
                capability=DOCUMENT_READINESS_CAPABILITY,
                description="Check original Run controlled effects required before document access",
                providers={
                    "platform": document_readiness_provider or UnavailableDocumentReadiness()
                },
                unbound_provider="platform",
            ),
            *_workspace_tool_definitions(contracts),
            _tool_definition(contracts,
                sequence_safe=True,
                capability="audit.export/v1",
                description=(
                    "Export selected saved Evidence and Proposal/Effect facts from this Run "
                    "directly to an immutable output Artifact. Use evidence_refs and proposal_refs "
                    "already returned by Tools; never rewrite original receipts to make an audit "
                    "file. Returns only path/hash/counts/references, not the full export. Publish "
                    "the returned artifact_ref using the existing approved document save when "
                    "required. This generic audit JSON is not a Skill-specific execution-result "
                    "schema or a business verdict."
                ),
                providers={"platform": audit_export_provider or AuditExportProvider(None)},
                unbound_provider="platform", minimum_execution_profile="SUPERVISED",
            ),
            _tool_definition(contracts,
                capability="tool.sequence/v1",
                description="Execute 1-5 already-determined read or local calls in order, with fixed arguments and optional exact result checks. Each call retains its own permission, audit and tool budget. Stops at the first error or failed check. Does not accept effects, interaction, nested sequences or model dispatch. Do not cross required external-saving or reasoning checkpoints.",
                providers={"platform": ToolSequenceProvider()}, unbound_provider="platform",
                minimum_execution_profile="SUPERVISED", sequence_safe=False,
            ),
            _interaction_tool_definition(contracts),
            *((_change_propose_tool_definition(contracts),)
              if deferred_features_enabled or database_writes_enabled
              or document_writes_enabled or git_writes_enabled or mcp_tools_enabled else ()),
            *(_subagent_tool_definitions(contracts, subagent_provider)
              if deferred_features_enabled else ()),
        )
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
        _tool_definition(contracts,
            capability=SUBAGENT_DISPATCH_CAPABILITY,
            sequence_safe=False,
            description=(
                "Fan out bounded read-only sub-analyses of independent aspects and collect "
                "their conclusions; sub-agents cannot write, ask, propose, or fan out again"
            ),
            providers={"platform": provider},
            unbound_provider="platform",
            minimum_execution_profile="SUPERVISED",
        ),
    )


def _interaction_tool_definition(contracts: ContractStore) -> ToolDefinition:
    """ユーザー入力前に SDK を停止する platform control Tool を登録する。"""

    return _tool_definition(contracts,
        capability=INTERACTION_REQUEST_CAPABILITY,
        description=(
            "Pause this run and request structured user input when a required fact, choice, "
            "review, or approval cannot be decided safely"
        ),
        providers={"platform": DeferredInteractionProvider()},
        unbound_provider="platform",
        minimum_execution_profile="GUIDED",
        defer_execution=True,
    )


def _change_propose_tool_definition(contracts: ContractStore) -> ToolDefinition:
    """Agent の提案を外部 write から切り離して Worker へ defer する control Tool。"""

    # 直接登録しない Effect の payload 契約も、提案者が知る必要がある。
    # Provider 側と別の形式を発明せず、既存契約の Agent 向け説明を再利用する。
    payload_guidance = "\n".join(
        str(contracts.load(f"tools/{capability}/request.schema.json")["description"])
        for capability in ("database.write/v1", "document.write/v1")
    )
    return _tool_definition(contracts,
        capability=CHANGE_PROPOSE_CAPABILITY,
        description=(
            "Create a structured external change proposal; this never applies the change and "
            "Skillmind independently validates approval and scope. Use the exact resource slot key "
            "and authorized operation from the task brief. When operations are listed, omit "
            "effect_intent_key and use at least minimum_risk; for legacy declared intents, "
            "copy the exact intent key and risk. "
            "capability_version identifies the write effect declared by that resource "
            "(database.write/v1 for database rows, document.write/v1 for library documents), "
            "not this change.propose/v1 control tool. "
            "For database writes, first read using filters equal to the complete primary key, "
            "columns=[] and offset=0; the result must be untruncated. INSERT requires no rows, "
            "expected=null and revision=absent. UPDATE requires the complete observed row as "
            "expected and its row_hashes entry as revision (not the response content_hash). "
            "The /row SET value must contain exactly key, values and expected. Put revision "
            "only in precondition.revision, never inside that value object. "
            "For UPDATE, copy the entire observed row unchanged into expected, including all "
            "primary-key, generated/identity and null-valued fields. expected is a read-only "
            "comparison snapshot, not the columns to write. Put primary-key fields in key "
            "and exclude them from values; omit generated/identity columns from values only. "
            "Include that exact read's Evidence reference. Rollback text does not authorize "
            "DELETE or any other compensation.\n" + payload_guidance
        ),
        providers={"platform": DeferredChangeProposalProvider()},
        unbound_provider="platform",
        minimum_execution_profile="GUIDED",
        defer_execution=True,
    )


def _workspace_tool_definitions(contracts: ContractStore) -> tuple[ToolDefinition, ...]:
    """Run 内の読取と制限付き書込を精確な capability version ごとに登録する。"""

    return (
        _tool_definition(contracts,
            sequence_safe=True,
            capability="json.schema.validate/v1",
            description=("Validate Run-local JSON against a Draft 2020-12 Schema, including "
                         "format assertions. Read schema_path and instance_path from "
                         "input/workspace/output only; fragment refs within the schema are "
                         "allowed, external refs are denied. Returns exact file hashes and "
                         "bounded JSON Pointer errors. Does not validate business semantics."),
            providers={"workspace": JsonSchemaValidateProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="GUIDED",
        ),
        _tool_definition(contracts,
            sequence_safe=True,
            capability="workspace.read/v1",
            description="Read one UTF-8 file from the isolated Run workspace",
            providers={"workspace": WorkspaceReadProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="GUIDED",
        ),
        _tool_definition(contracts,
            sequence_safe=True,
            capability="workspace.search/v1",
            description="Search UTF-8 files in the isolated Run workspace",
            providers={"workspace": WorkspaceSearchProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
        _tool_definition(contracts,
            sequence_safe=True,
            capability="workspace.write/v1",
            description="Write one UTF-8 file to the isolated Run workspace or output",
            providers={"workspace": WorkspaceWriteProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
        _tool_definition(contracts,
            sequence_safe=True,
            capability="workspace.write/v2",
            description=(
                "Write one UTF-8 file within the isolated Run; output files receive an "
                "immutable Artifact reference only after their exact bytes are committed"
            ),
            providers={"workspace": WorkspaceWriteProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
    )
