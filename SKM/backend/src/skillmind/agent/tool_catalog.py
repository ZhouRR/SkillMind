"""Provider と versioned 契約を同じ capability から装配する唯一の Tool catalog。"""

from __future__ import annotations

from collections.abc import Mapping

from skillmind.agent.artifact_append import ArtifactAppendProvider
from skillmind.agent.artifact_materialize import ArtifactMaterializeProvider
from skillmind.agent.audit_export import AuditExportProvider
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.control_providers import (
    DeferredChangeProposalProvider,
    DeferredInteractionProvider,
    UnavailableDocumentReadiness,
)
from skillmind.agent.document_files import DocumentFilesProvider
from skillmind.agent.document_inspection import DocumentInspectProvider
from skillmind.agent.document_listing import DocumentListProvider
from skillmind.agent.document_provider import DocumentConvertProvider, DocumentProvider
from skillmind.agent.json_schema_provider import JsonSchemaValidateProvider
from skillmind.agent.repository_provider import RepositoryReadProvider
from skillmind.agent.repository_source import (
    RepositorySnapshotSource,
)
from skillmind.agent.repository_workspace import RepositoryWorkspaceProvider
from skillmind.agent.subagent import SUBAGENT_DISPATCH_CAPABILITY
from skillmind.agent.tool_gateway import (
    ToolDefinition,
    ToolProvider,
    ToolRegistry,
)
from skillmind.agent.tool_sequence import ToolSequenceProvider
from skillmind.agent.workspace_edit import WorkspaceEditProvider
from skillmind.agent.workspace_image import WorkspaceImageProvider
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

    if capability in {"repository.read/v1", "mcp.tools/v1", "mcp.query/v1", "mcp.read/v1"}:
        from skillmind.agent.resource_files import ResourceFileProvider
        schema = contracts.load(f"tools/{capability}/request.schema.json")
        inline = {**schema["oneOf"][0], **({"$defs": schema["$defs"]} if "$defs" in schema else {})}
        providers = {
            key: ResourceFileProvider(provider, inline) for key, provider in providers.items()
        }
        description += (
            " Prefer response_mode=file; complete data stays in the Run workspace. "
            "A JSON request_file with expected_hash may replace inline arguments. "
            "Native MCP arguments are passed without renaming their fields."
        )
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
    native_database_provider: ToolProvider | None = None,
    http_provider: ToolProvider | None = None,
    redmine_issue_provider: ToolProvider | None = None,
    database_provider: ToolProvider | None = None,
    mcp_provider: ToolProvider | None = None,
    mcp_tools_provider: ToolProvider | None = None,
    mcp_query_provider: ToolProvider | None = None,
    mcp_download_provider: ToolProvider | None = None,
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
            "Write calls use the mcp.call/v1 proposal format documented on change.propose/v1.",
        ),
        (
            "mcp.query/v1",
            mcp_query_provider,
            "Call tools explicitly authorized as read in the frozen catalog. Other tools require "
            "change.propose approval; never call them here.",
        ),
        (
            "mcp.download/v1", mcp_download_provider,
            "Download original bytes from the same origin as this MCP connection, using a "
            "source_evidence_ref from a successful mcp.query and an exact JSON pointer in its "
            "response. No arbitrary URLs, redirects or cross-connection references. "
            "Returns a Run-local path/hash; call workspace.image/v1 to actually view images.",
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
    if native_database_provider is not None:
        definitions.append(_tool_definition(
            contracts, sequence_safe=True, capability="database.query/v1",
            description=(
                'Execute one native PostgreSQL SELECT with positional $1 parameters using the '
                'bound database account. JOIN/CTE/schema queries are supported. Parameters are '
                'JSON scalar values; use explicit SQL casts when needed (for example '
                '$1::text::uuid or $1::text::jsonb). Complete bounded '
                'results are saved as a local JSON file; inspect rows with workspace tools. '
                'Mutating SQL requires database.execute/v1 approval. Database roles enforce '
                'table/column access.'
            ),
            providers={"postgres": native_database_provider}))
    if http_provider is not None:
        definitions.append(_tool_definition(
            contracts, sequence_safe=True, capability="http.read/v1",
            description=(
                'Request GET/HEAD from the bound HTTP API using a relative path. Tool success '
                'does not imply HTTP success: inspect http_status. Complete '
                'response bytes are saved as a Run file; read them with workspace tools. '
                'Mutations require http.write/v1 approval. Never supply credentials or a full '
                'URL.'
            ),
            providers={"http": http_provider}))
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
        definitions.append(_tool_definition(contracts, capability="repository.workspace/v1",
            description=(
                'Check out scoped Git text files to an editable Run directory. prepare_commit '
                'reads exact local file bytes and writes a proposal JSON file without '
                'retranscribing content. Submit that file using change.propose '
                'request_file/expected_hash plus evidence_refs; remote updates still require '
                'approval and read-back.'
            ),
            providers={"git": RepositoryWorkspaceProvider(repository_source)},
            minimum_execution_profile="SUPERVISED"))
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

    return _tool_definition(
        contracts,
        sequence_safe=True,
        capability="document.read/v1",
        description=(
            "Read a project document by path or document_id. When the Run grants project-wide "
            "reading, list/read additional project files, including newly uploaded receipts, "
            "without changing the selected task inputs. Selected inputs keep their frozen bytes. "
            "Use expected_hash for an exact observed/uploaded version. Prefer response_mode=file: "
            "copies the "
            "verified original bytes to this Run and returns a local path/hash without body. "
            "Read or search the file as needed. When available, use workspace.edit/v1 to copy, "
            "append, "
            "edit or publish without repeating its full text. Inline remains available for "
            "short reads. Local file changes do not update the library; save using the approved "
            "document write flow."
        ),
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
            "artifact_refs and artifact size/hash instead of rewriting the Markdown. "
            "When workspace.read/v1 is allowed, use response_mode=file with publish_artifact=true: "
            "returns a readable file path, checksum and line count without the full body. "
            "Read that file with workspace.read offset=0 and expected_hash; the default page "
            "is 64000 characters, adjustable up to 200000. "
            "follow next_offset until null to cover the full document. If the local copy is "
            "missing or changed, restore the same artifact_ref using artifact.materialize/v1. "
            "Inline is the legacy fallback when file reading is unavailable."
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
            "Inspect storage LastModified, Version ID and ETag of one frozen input document "
            "without downloading its bytes. This does not inspect new output destinations; "
            "when available, use document.files stat/prepare for the output library."
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
            "Page through authorized project documents by directory and storage LastModified. "
            "Runs with project-wide reading include unselected and newly uploaded files. "
            "Check response.scope; historical Runs may cover only frozen inputs. "
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
    document_files_provider: ToolProvider | None = None,
    audit_export_provider: ToolProvider | None = None,
    artifact_append_provider: ToolProvider | None = None,
    artifact_materialize_provider: ToolProvider | None = None,
    native_database_provider: ToolProvider | None = None,
    http_provider: ToolProvider | None = None,
    redmine_issue_provider: ToolProvider | None = None,
    database_provider: ToolProvider | None = None,
    mcp_provider: ToolProvider | None = None,
    mcp_tools_provider: ToolProvider | None = None,
    mcp_query_provider: ToolProvider | None = None,
    mcp_download_provider: ToolProvider | None = None,
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
                native_database_provider=native_database_provider,
                http_provider=http_provider,
                redmine_issue_provider=redmine_issue_provider,
                database_provider=database_provider,
                mcp_provider=mcp_provider,
                mcp_tools_provider=mcp_tools_provider,
                mcp_query_provider=mcp_query_provider,
                mcp_download_provider=mcp_download_provider,
                repository_source=repository_source,
            ),
            document_read_tool_definition(contracts, document_source),
            _tool_definition(
                contracts,
                sequence_safe=True,
                capability="document.files/v1",
                description=(
                    "List/stat the authorized library's current files and directories "
                    "using library_key (the output resource_key) and relative paths. stat returns "
                    "state.revision; use kind=directory for folders, trashed=true and document_id "
                    "for a specific recycled file. For action=prepare supply operation, path and "
                    "purpose. CREATE/CREATE_FOLDER need no expected_revision; all other operations "
                    "use the observed state.revision, not the content hash or listing revision. "
                    "CREATE/UPDATE also need a Run artifact_ref and mime_type; MOVE/MOVE_FOLDER "
                    "need destination; RESTORE identifies the recycled document_id. "
                    "The tool resolves original Artifact hash/size. Submit the returned "
                    "proposal to change.propose with this response's evidence_refs. "
                    "Preparation alone does not save or modify anything. Follow listing "
                    "next_offset with expected_revision; frozen document reads remain separate."
                ),
                providers={"platform": document_files_provider or DocumentFilesProvider()},
                unbound_provider="platform",
                minimum_execution_profile="SUPERVISED",
            ),
            document_convert_tool_definition(
                contracts, document_source, observations=document_observations
            ),
            document_inspect_tool_definition(contracts, document_source),
            document_list_tool_definition(contracts, document_source),
            _tool_definition(
                contracts,
                sequence_safe=True,
                capability=DOCUMENT_READINESS_CAPABILITY,
                description="Check original Run controlled effects required before document access",
                providers={
                    "platform": document_readiness_provider or UnavailableDocumentReadiness()
                },
                unbound_provider="platform",
            ),
            *_workspace_tool_definitions(contracts),
            _tool_definition(
                contracts,
                sequence_safe=True,
                capability="artifact.materialize/v1",
                description=(
                    "Download exact saved UTF-8 bytes from an Artifact of this Run to a readable "
                    "workspace file. Returns file metadata only, never the body. "
                    "Use workspace.read "
                    "with offset=0 and expected_hash, following next_offset. The default page "
                    "is 64000 characters; max_chars can be adjusted up to 200000. "
                    "Restores missing or modified local copies without re-conversion "
                    "or publication."
                ),
                providers={
                    "platform": artifact_materialize_provider or ArtifactMaterializeProvider(None)
                },
                unbound_provider="platform",
                minimum_execution_profile="GUIDED",
            ),
            _tool_definition(
                contracts,
                sequence_safe=True,
                capability="artifact.append/v1",
                description=(
                    "Append short text to a saved Artifact from this Run using artifact_ref. "
                    "Reads and verifies original bytes internally; provide only the suffix, "
                    "including desired newlines. Overwrites the same local output path and "
                    "returns a new Artifact reference, hash and size without returning the body. "
                    "Use that returned reference for document saving. The source ref is fixed: "
                    "repeating the same source and text does not append twice. Conversion "
                    "Artifact paths are logical names, not files readable by workspace.read. "
                    "Does not change external documents or existing audit references."
                ),
                providers={"platform": artifact_append_provider or ArtifactAppendProvider(None)},
                unbound_provider="platform",
                minimum_execution_profile="SUPERVISED",
            ),
            _tool_definition(
                contracts,
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
                unbound_provider="platform",
                minimum_execution_profile="SUPERVISED",
            ),
            _tool_definition(
                contracts,
                capability="tool.sequence/v1",
                description=(
                'Execute 1-5 already-determined read or local calls in order, with fixed '
                'arguments and optional exact result checks. Each call retains its own '
                'permission, audit and tool budget. Stops at the first error or failed check. '
                'Does not accept effects, interaction, nested sequences or model dispatch. Do not '
                'cross required external-saving or reasoning checkpoints.'
            ),
                providers={"platform": ToolSequenceProvider()},
                unbound_provider="platform",
                minimum_execution_profile="SUPERVISED",
                sequence_safe=False,
            ),
            _interaction_tool_definition(contracts),
            *(
                (_change_propose_tool_definition(contracts),)
                if deferred_features_enabled
                or database_writes_enabled
                or document_writes_enabled
                or git_writes_enabled
                or mcp_tools_enabled
                or http_provider is not None
                else ()
            ),
            *(
                _subagent_tool_definitions(contracts, subagent_provider)
                if deferred_features_enabled
                else ()
            ),
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

    from skillmind.effects.catalog import EFFECT_CAPABILITIES

    # Tool として直接公開しない Effect の形状も、同じ提案入口から読めるようにする。
    # 単一の Effect 登録表から列挙し、新能力や旧 Run 用契約の説明漏れを防ぐ。
    payload_guidance = "\n".join(
        f"{capability}: "
        + str(contracts.load(f"tools/{capability}/request.schema.json")["description"])
        for capability in sorted(EFFECT_CAPABILITIES)
    )
    return _tool_definition(contracts,
        capability=CHANGE_PROPOSE_CAPABILITY,
        description=(
            "Submit one controlled external operation. Supply resource_key, capability_version, "
            "operation, target, changes, precondition, summary and evidence_refs. Platform-derived "
            "idempotency_key, risk_level, verification, rollback, expiry and RESUME checkpoint may "
            "be omitted; include actual new business checkpoint facts when required. With explicit "
            "Run-start approval and direct-delivery support, the original committed receipt is "
            "returned in this call. Otherwise stop when paused and await the original operation. "
            "The model request alone never authorizes a change and "
            "Skillmind independently validates approval and scope. Use the exact resource slot key "
            "and authorized operation from the task brief. When operations are listed, omit "
            "effect_intent_key and use at least minimum_risk; for legacy declared intents, "
            "copy the exact intent key and risk. "
            "capability_version identifies the write effect declared by that resource "
            "(database.execute/v1 for SQL, http.write/v1 for HTTP APIs, "
            "document.write/v1 for documents), not this change.propose/v1 control tool. "
            "Include the original read's Evidence reference. Alternatively supply request_file, "
            "expected_hash and optional evidence_refs for an exact prepared JSON proposal in "
            "workspace/ or output/. The Worker loads and validates that file "
            "before approval; it does not publish edited files automatically. "
            "Read-back must validate the original business requirements. "
            "Rollback text alone does not authorize compensation. "
            "The formats below describe supported effects, not permission; "
            "use only the capabilities and operations authorized in this Run.\n" + payload_guidance
        ),
        providers={"platform": DeferredChangeProposalProvider()},
        unbound_provider="platform",
        minimum_execution_profile="GUIDED",
        defer_execution=True,
    )


def _workspace_tool_definitions(contracts: ContractStore) -> tuple[ToolDefinition, ...]:
    """Run 内の読取と制限付き書込を精確な capability version ごとに登録する。"""

    return (
        _tool_definition(
            contracts, capability="workspace.image/v1",
            description=(
                "View a Run-local PNG, JPEG or WebP as actual model image content. Supply path "
                "and the observed expected_hash. Metadata/path/Base64 text alone is not a viewed "
                "image. Maximum 8 MiB and 25 million pixels; no SVG or animations. "
                "Use registered download/read tools to obtain files first. Image interpretation "
                "does not authorize coordinate clicks or guess selectors."
            ),
            providers={"workspace": WorkspaceImageProvider()}, unbound_provider="workspace",
            minimum_execution_profile="GUIDED", sequence_safe=False,
        ),
        _tool_definition(
            contracts,
            sequence_safe=True,
            capability="json.schema.validate/v1",
            description=(
                "Validate Run-local JSON against a Draft 2020-12 Schema, including "
                "format assertions. Read schema_path and instance_path from "
                "input/workspace/output only; fragment refs within the schema are "
                "allowed, external refs are denied. Returns exact file hashes and "
                "bounded JSON Pointer errors. Does not validate business semantics."
            ),
            providers={"workspace": JsonSchemaValidateProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="GUIDED",
        ),
        _tool_definition(
            contracts,
            sequence_safe=True,
            capability="workspace.read/v1",
            description=(
                "Read one UTF-8 Run file. Use offset=0 and expected_hash; the default page "
                "is 64000 Unicode characters. Adjust max_chars up to 200000 for broad reading "
                "when context allows, or request a smaller range for a focused lookup. "
                "Follow next_offset until null for complete coverage. Offsets count Unicode "
                "characters, not bytes. Line ranges are an alternative. Never infer full "
                "coverage from a truncated response or search matches alone."
            ),
            providers={"workspace": WorkspaceReadProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="GUIDED",
        ),
        _tool_definition(
            contracts,
            sequence_safe=True,
            capability="workspace.search/v1",
            description="Search UTF-8 files in the isolated Run workspace",
            providers={"workspace": WorkspaceSearchProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
        _tool_definition(
            contracts,
            sequence_safe=True,
            capability="workspace.write/v1",
            description="Write one UTF-8 file to the isolated Run workspace or output",
            providers={"workspace": WorkspaceWriteProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
        _tool_definition(
            contracts,
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
        _tool_definition(
            contracts,
            sequence_safe=True,
            capability="workspace.edit/v1",
            description=(
                "Copy, append, precisely edit or publish a Run file by path and expected "
                "source hash. A different destination must be absent; edit an existing target "
                "in place with its own hash. Original text stays in the file; "
                "only send new text or "
                "unique replacements. output/ targets publish verified Artifact bytes. "
                "This does not change the document library; submit its Artifact through "
                "the approved document write flow."
            ),
            providers={"workspace": WorkspaceEditProvider()},
            unbound_provider="workspace",
            minimum_execution_profile="SUPERVISED",
        ),
    )
