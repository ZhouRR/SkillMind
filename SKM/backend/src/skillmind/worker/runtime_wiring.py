"""実行 Worker の Provider、Engine、Effect を明示的に組み立てる。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from skillmind.agent.artifact_append import ArtifactAppendProvider
from skillmind.agent.artifact_materialize import ArtifactMaterializeProvider
from skillmind.agent.audit_export import AuditExportProvider
from skillmind.agent.audit_source import PostgresAuditExportSource
from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.codex_engine import CodexAgentSdkEngine
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration
from skillmind.agent.context_builder import (
    ProductionRunContextBuilder,
)
from skillmind.agent.contract_store import (
    ContractStore,
)
from skillmind.agent.database_provider import DatabaseReadProvider
from skillmind.agent.document_files import DocumentFilesProvider
from skillmind.agent.document_readiness import DocumentReadinessProvider
from skillmind.agent.domain import AgentEngine, RunContext
from skillmind.agent.engine import ClaudeAgentSdkEngine, RunMcpRuntime
from skillmind.agent.evidence import PostgresToolAuditWriter
from skillmind.agent.http_provider import HttpReadProvider
from skillmind.agent.http_source import HttpResourceSource
from skillmind.agent.mcp_download import McpDownloadProvider
from skillmind.agent.mcp_lease import McpDesktopLeases
from skillmind.agent.mcp_provider import McpReadProvider
from skillmind.agent.mcp_source import StreamableHttpMcpSource
from skillmind.agent.mcp_tools_provider import McpToolsProvider
from skillmind.agent.mcp_tools_source import StreamableHttpMcpToolsSource
from skillmind.agent.model_result import PostgresResultAssemblyLookup
from skillmind.agent.postgres_native import NativePostgresSource
from skillmind.agent.postgres_native_provider import NativeDatabaseProvider
from skillmind.agent.postgres_source import PostgresDatabaseSource
from skillmind.agent.repository_client import (
    GitCommandRepositoryClient,
    SvnCommandRepositoryClient,
)
from skillmind.agent.repository_source import IntegrationRepositorySnapshotSource
from skillmind.agent.result_references import PostgresEffectSummaryLookup
from skillmind.agent.result_validation import (
    PostgresArtifactLookup,
    PostgresEvidenceLookup,
    PostgresProposalLookup,
    ResultValidator,
)
from skillmind.agent.session_store import PostgresSessionTranscriptBackend
from skillmind.agent.subagent_provider import SubagentDispatchProvider
from skillmind.agent.subagent_sessions import PostgresSubagentSessionRecorder
from skillmind.agent.tool_catalog import (
    create_run_tool_registry,
)
from skillmind.agent.tool_gateway import RunToolRuntime
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.core.secret_crypto import load_secret_cipher
from skillmind.core.settings import Settings
from skillmind.documents.library import DocumentLibraryTarget
from skillmind.documents.observation_repository import PostgresDocumentObservationLookup
from skillmind.documents.source import (
    DatabaseProjectDocumentInventory,
    DatabaseProjectDocumentSource,
)
from skillmind.effects.document_service import DocumentEffectService
from skillmind.effects.git_receipt import GitCommitReader
from skillmind.effects.postgres_write import PostgresDatabaseWriteSource
from skillmind.effects.reconciliation_execution import EffectReconciliationExecutor
from skillmind.effects.reconciliation_service import EffectReconciliationService
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.wiring import create_effect_provider_registry
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.runs.proposal_continuation import ProposalContinuationReader
from skillmind.runs.realtime import RedisPublisher, RedisRunRealtimePublisher
from skillmind.runs.repository_inputs import PostgresInputSnapshotStore
from skillmind.skills.realtime import RedisInterpretEventPublisher
from skillmind.skills.service_wiring import build_skill_service
from skillmind.skills.wiring import build_skill_interpreter
from skillmind.storage import FileStorage
from skillmind.storage.factory import (
    create_document_upload_limits,
    create_document_write_source,
)
from skillmind.worker.effects import ApprovedEffectExecutor
from skillmind.worker.executor import AgentRunExecutor
from skillmind.worker.inline_effects import InlineEffectCoordinator
from skillmind.worker.proposal_preflight import ProposalPreflight
from skillmind.worker.tool_authority import require_tool_authority


def configure_execution_runtime(
    ctx: dict[str, Any],
    *,
    settings: Settings,
    features: ExecutionFeatures,
    file_storage: FileStorage,
    document_library_target: DocumentLibraryTarget | None,
) -> None:
    """共有台帳に実行 port を配線し、保守 Worker ではこの組立を呼ばない。

    Provider と Engine は同じ認可・回執実装を共有する。ここでは外部 I/O や
    状態遷移を行わず、実行時の lease と承認検証は各 service に委ねる。
    """

    secret_resolver = DeploymentSecretResolver(
        cipher=load_secret_cipher(settings.managed_secret_kek)
    )
    document_receipt_source = (
        create_document_write_source(settings, storage=file_storage)
        if document_library_target is not None
        else None
    )
    ctx["reconciliation_executor"] = EffectReconciliationExecutor(
        requests=ctx["reconciliation_requests"],
        reader=EffectReconciliationService(
            ctx["database_session_factory"],
            secret_resolver=secret_resolver,
            database_reader=PostgresDatabaseWriteSource(),
            git_reader=GitCommitReader(GitCommandRepositoryClient(command_timeout_seconds=30)),
            document_reader=document_receipt_source,
            document_library_target=document_library_target,
        ),
    )
    contracts = ContractStore(settings.contracts_dir)
    document_source = DatabaseProjectDocumentSource(
        ctx["database_session_factory"],
        file_storage=file_storage,
    )
    # 実 git/svn client。物化器と repository.read/v1 Provider が同じ source を共有し、
    # 凭据解決と scope 検証を一箇所に閉じ込める。承認済み書き込みも
    # 同じ git client instance を使い、subprocess 境界と凭据の扱いを一本化する。
    git_client = GitCommandRepositoryClient(
        command_timeout_seconds=settings.repository_command_timeout_seconds
    )
    svn_client = SvnCommandRepositoryClient(
        command_timeout_seconds=settings.repository_command_timeout_seconds
    )
    repository_source = IntegrationRepositorySnapshotSource(
        ctx["database_session_factory"],
        clients={
            "git": git_client,
            "svn": svn_client,
        },
        secret_resolver=secret_resolver,
    )
    materializer = WorkspaceMaterializer(
        document_inventory=DatabaseProjectDocumentInventory(source=document_source),
        input_snapshots=PostgresInputSnapshotStore(ctx["database_session_factory"]),
        max_bytes=settings.workspace_materialize_max_bytes,
        max_files=settings.workspace_materialize_max_files,
        max_total_bytes=settings.workspace_materialize_total_max_bytes,
        max_total_files=settings.workspace_materialize_total_max_files,
        repository_source=repository_source,
    )
    # engine → registry → 扇出 Provider → engine と参照が循環する。Provider には engine 実体では
    # なく取得関数を渡し、解決を呼び出し時まで遅らせて組み立て順への依存を切る。
    engine_holder: dict[str, AgentEngine] = {}
    result_validator = ResultValidator(
        PostgresEvidenceLookup(ctx["database_session_factory"]),
        PostgresProposalLookup(ctx["database_session_factory"]),
        effect_lookup=PostgresEffectSummaryLookup(ctx["database_session_factory"]),
        artifact_lookup=PostgresArtifactLookup(ctx["database_session_factory"]),
        assembly_lookup=PostgresResultAssemblyLookup(ctx["database_session_factory"]),
    )
    audit_source = PostgresAuditExportSource(ctx["database_session_factory"])
    database_provider = DatabaseReadProvider(
        ctx["database_session_factory"],
        source=PostgresDatabaseSource(),
        secret_resolver=secret_resolver,
    )
    registry = create_run_tool_registry(
        contracts,
        deferred_features_enabled=features.deferred,
        database_writes_enabled=features.database_writes,
        document_writes_enabled=features.document_writes,
        git_writes_enabled=features.git_writes,
        mcp_tools_enabled=features.mcp_tools,
        subagent_provider=SubagentDispatchProvider(
            engine=lambda: engine_holder["engine"],
            branch_timeout_seconds=settings.subagent_branch_timeout_seconds,
            session_recorder=PostgresSubagentSessionRecorder(ctx["database_session_factory"]),
            result_validator=result_validator,
        )
        if settings.deferred_features_enabled
        else None,
        document_source=document_source,
        document_observations=PostgresDocumentObservationLookup(ctx["database_session_factory"]),
        document_readiness_provider=DocumentReadinessProvider(ctx["database_session_factory"]),
        document_files_provider=DocumentFilesProvider(
            ctx["database_session_factory"],
            target=document_library_target,
        ),
        audit_export_provider=AuditExportProvider(audit_source),
        artifact_materialize_provider=ArtifactMaterializeProvider(audit_source),
        artifact_append_provider=ArtifactAppendProvider(audit_source),
        mcp_provider=McpReadProvider(
            ctx["database_session_factory"],
            source=StreamableHttpMcpSource(),
            secret_resolver=secret_resolver,
        ),
        mcp_tools_provider=McpToolsProvider(
            ctx["database_session_factory"],
            capability="mcp.tools/v1",
            source=StreamableHttpMcpToolsSource(),
            secret_resolver=secret_resolver,
            leases=McpDesktopLeases(ctx["database_session_factory"]),
        )
        if features.mcp_tools
        else None,
        mcp_query_provider=McpToolsProvider(
            ctx["database_session_factory"],
            capability="mcp.query/v1",
            source=StreamableHttpMcpToolsSource(),
            secret_resolver=secret_resolver,
            leases=McpDesktopLeases(ctx["database_session_factory"]),
        )
        if features.mcp_tools
        else None,
        mcp_download_provider=McpDownloadProvider(
            ctx["database_session_factory"],
            source=HttpResourceSource(),
            secret_resolver=secret_resolver,
        )
        if features.mcp_tools
        else None,
        database_provider=database_provider,
        native_database_provider=NativeDatabaseProvider(
            ctx["database_session_factory"],
            source=NativePostgresSource(),
            secret_resolver=secret_resolver,
        ),
        http_provider=HttpReadProvider(
            ctx["database_session_factory"],
            source=HttpResourceSource(),
            secret_resolver=secret_resolver,
        ),
        repository_source=repository_source,
    )

    def create_authorized_gateway(context: RunContext) -> RunToolRuntime:
        """現在の Worker claim を一度だけ捕捉し、別 Run の audit writer を共有しない。"""

        authority = require_tool_authority(context)
        runtime = registry.build_gateway_runtime(
            context,
            audit_writer=PostgresToolAuditWriter(
                ctx["database_session_factory"],
                claimed_run=authority.claimed,
                authority_check=authority.require_active,
            ),
        )
        preflight = ProposalPreflight(ctx["run_service"], context, authority)
        runtime = replace(
            runtime, mcp=replace(runtime.mcp, on_deferred_validation=preflight.validate)
        )
        executor = ctx.get("effect_executor")
        if (
            settings.agent_sdk == "codex"
            and getattr(settings, "inline_effects_enabled", False)
            and isinstance(executor, ApprovedEffectExecutor)
        ):
            handler = InlineEffectCoordinator(ctx["run_service"], executor, context, authority)
            runtime = replace(runtime, mcp=replace(runtime.mcp, on_inline_effect=handler.invoke))
        return runtime

    def create_authorized_runtime(context: RunContext) -> RunMcpRuntime:
        """Claude は同じ認可済み Gateway の in-process MCP adapter を使う。"""

        return create_authorized_gateway(context).mcp

    engine: AgentEngine
    selected_model: str | None
    if settings.agent_sdk == "codex":
        codex_configuration = CodexRuntimeConfiguration(
            settings.codex_model,
            settings.codex_reasoning_effort,
            settings.codex_home,
        )
        selected_model = codex_configuration.primary_model
        engine = CodexAgentSdkEngine(
            configuration=codex_configuration,
            runtime_factory=create_authorized_gateway,
            transcript_backend=PostgresSessionTranscriptBackend(ctx["database_session_factory"]),
        )
    else:
        runtime_configuration = ClaudeRuntimeConfiguration.from_environ()
        selected_model = runtime_configuration.primary_model
        engine = ClaudeAgentSdkEngine(
            mcp_server_factory=create_authorized_runtime,
            configuration=runtime_configuration,
            session_store=ctx["session_store"],
        )
    context_builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(settings.run_workspace_root),
        tool_registry=registry,
        model=selected_model,
        materializer=materializer,
        execution_features=features,
        document_library_target=document_library_target,
        proposal_continuations=ProposalContinuationReader(ctx["database_session_factory"]),
        database_observations=database_provider,
        contracts=contracts,
        interaction_replay=settings.agent_sdk != "codex",
    )
    engine_holder["engine"] = engine
    ctx["run_executor"] = AgentRunExecutor(
        run_service=ctx["run_service"],
        context_builder=context_builder,
        engine=engine,
        result_validator=result_validator,
        lease_seconds=settings.run_lease_seconds,
        preparation_timeout_seconds=settings.run_preparation_timeout_seconds,
        max_attempts=settings.run_max_attempts,
        realtime_publisher=RedisRunRealtimePublisher(cast(RedisPublisher, ctx["redis"])),
    )
    if features.effects_enabled:
        document_effect_service = None
        document_effect_source = None
        if features.document_writes:
            if document_library_target is None:
                raise ValueError("Document effect requires the configured project library")
            document_effect_source = document_receipt_source
            document_effect_service = DocumentEffectService(
                ctx["database_session_factory"],
                features=features,
                target=document_library_target,
                limits=create_document_upload_limits(settings),
                document_source=document_source,
            )
        ctx["effect_executor"] = ApprovedEffectExecutor(
            effect_service=ctx["effect_service"],
            provider_registry=create_effect_provider_registry(
                workspace_root=settings.run_workspace_root,
                features=features,
                effect_service=ctx["effect_service"],
                secret_resolver=secret_resolver,
                git_client=git_client,
                svn_client=svn_client,
                document_service=document_effect_service,
                document_source=document_effect_source,
                mcp_leases=McpDesktopLeases(ctx["database_session_factory"]),
            ),
            # 承認済み apply も MANAGED 凭据を使うため、読取 Provider と同じ KEK cipher を渡す。
            # 渡し漏れると「読めるのに承認後の書き込みだけ失敗する」非対称な障害になる。
            secret_resolver=secret_resolver,
            worker_id=ctx["worker_id"],
            lease_seconds=settings.run_lease_seconds,
            max_attempts=settings.run_max_attempts,
        )
    # Skill interpret を Worker 側で実行する。model への egress は Worker だけが持つ。
    interpreter_components = build_skill_interpreter(settings)
    ctx["skill_service"] = build_skill_service(
        settings,
        interpreter_components=interpreter_components,
        session_factory=ctx["database_session_factory"],
        file_storage=file_storage,
        document_library_target=document_library_target,
    )
    ctx["interpret_publisher"] = RedisInterpretEventPublisher(cast(RedisPublisher, ctx["redis"]))
