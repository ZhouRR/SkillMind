"""ARQ Worker の lifecycle、job、実行制約を定義する。"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from uuid import UUID

from arq import cron
from arq.connections import ArqRedis, RedisSettings
from arq.cron import CronJob
from arq.worker import Function
from arq.worker import func as arq_function
from sqlalchemy.ext.asyncio import AsyncEngine

from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.context_builder import (
    ContractStore,
    ProductionRunContextBuilder,
    create_run_tool_registry,
)
from skillmind.agent.database_provider import DatabaseReadProvider
from skillmind.agent.document_readiness import DocumentReadinessProvider
from skillmind.agent.domain import RunContext
from skillmind.agent.engine import ClaudeAgentSdkEngine, RunMcpRuntime
from skillmind.agent.evidence import PostgresToolAuditWriter
from skillmind.agent.mcp_provider import McpReadProvider
from skillmind.agent.mcp_source import StreamableHttpMcpSource
from skillmind.agent.postgres_source import PostgresDatabaseSource
from skillmind.agent.redmine_provider import RedmineIssueReadProvider
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
from skillmind.agent.session_store import PostgresSessionStore
from skillmind.agent.subagent_provider import SubagentDispatchProvider
from skillmind.agent.subagent_sessions import PostgresSubagentSessionRecorder
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.core.logging import configure_logging, log_event
from skillmind.core.secret_crypto import load_secret_cipher
from skillmind.core.settings import Settings, get_settings
from skillmind.db.resources import create_database_engine, create_session_factory
from skillmind.documents.library import configured_document_library
from skillmind.documents.observation_repository import PostgresDocumentObservationLookup
from skillmind.documents.source import (
    DatabaseProjectDocumentInventory,
    DatabaseProjectDocumentSource,
)
from skillmind.effects.document_service import DocumentEffectService
from skillmind.effects.postgres_write import PostgresDatabaseWriteSource
from skillmind.effects.reconciliation_execution import EffectReconciliationExecutor
from skillmind.effects.reconciliation_request_service import ReconciliationRequestService
from skillmind.effects.reconciliation_requests import (
    RECONCILIATION_DISPATCH_TOPIC,
    ReconciliationRequestNotFoundError,
)
from skillmind.effects.reconciliation_service import EffectReconciliationService
from skillmind.effects.redmine import (
    UrllibRedmineTransport,
)
from skillmind.effects.release import configured_execution_features
from skillmind.effects.service import EffectService
from skillmind.effects.wiring import create_effect_provider_registry
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.runs.domain import PendingOutboxMessage
from skillmind.runs.outbox import OutboxRelay
from skillmind.runs.realtime import RedisPublisher, RedisRunRealtimePublisher
from skillmind.runs.repository_inputs import PostgresInputSnapshotStore
from skillmind.runs.service import RunService
from skillmind.schedules import ScheduleService
from skillmind.skills import (
    SkillService,
)
from skillmind.skills.interpretation_requests import InterpretationRequestNotFoundError
from skillmind.skills.interpreter_execution import InterpretProgressCallback
from skillmind.skills.realtime import RedisInterpretEventPublisher
from skillmind.skills.request_service import (
    INTERPRETATION_DISPATCH_TOPIC,
    InterpretationRequestService,
)
from skillmind.skills.wiring import build_skill_interpreter
from skillmind.storage.factory import (
    create_document_upload_limits,
    create_document_write_source,
    create_file_storage,
)
from skillmind.worker.effects import ApprovedEffectExecutor
from skillmind.worker.executor import AgentRunExecutor, RunExecutor
from skillmind.worker.tool_authority import require_tool_authority

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Worker process で共有する Database engine を初期化する。"""

    settings = get_settings()
    features = configured_execution_features(settings)
    configure_logging(settings.log_level)
    # MANAGED SecretReference 復号用の KEK cipher。未設定なら MANAGED は fail closed で解決不能。
    secret_cipher = load_secret_cipher(settings.managed_secret_kek)
    ctx["database_engine"] = create_database_engine(settings)
    ctx["database_session_factory"] = create_session_factory(ctx["database_engine"])
    ctx["session_store"] = PostgresSessionStore.from_session_factory(
        ctx["database_session_factory"]
    )
    file_storage = create_file_storage(settings)
    document_library_target = configured_document_library(
        file_storage, bucket=settings.object_storage_bucket
    )
    ctx["run_service"] = RunService(
        ctx["database_session_factory"],
        deferred_features_enabled=features.deferred,
        database_writes_enabled=features.database_writes,
        document_writes_enabled=features.document_writes,
        document_library_target=document_library_target,
    )
    ctx["effect_service"] = EffectService(
        ctx["database_session_factory"],
        document_library_target=document_library_target,
        execution_features=features,
    )
    # 停写後も原結果を只読で核対できる。writer gate は借りず、外部 port は lookup だけ使う。
    ctx["reconciliation_requests"] = ReconciliationRequestService(
        ctx["database_session_factory"], document_library_target=document_library_target,
    )
    document_receipt_source = (
        create_document_write_source(settings, storage=file_storage)
        if document_library_target is not None else None
    )
    ctx["reconciliation_executor"] = EffectReconciliationExecutor(
        requests=ctx["reconciliation_requests"],
        reader=EffectReconciliationService(
            ctx["database_session_factory"],
            secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
            database_reader=PostgresDatabaseWriteSource(),
            document_reader=document_receipt_source,
            document_library_target=document_library_target,
        ),
    )
    ctx["outbox_relay"] = OutboxRelay(
        ctx["database_session_factory"], batch_size=settings.outbox_batch_size
    )
    ctx["settings"] = settings
    ctx["worker_id"] = f"{socket.gethostname()}-{os.getpid()}"
    log_event(
        logger,
        logging.INFO,
        "worker.started",
        worker_id=ctx["worker_id"],
    )
    contracts = ContractStore(settings.contracts_dir)
    document_source = DatabaseProjectDocumentSource(
        ctx["database_session_factory"],
        file_storage=file_storage,
    )
    # 実 git/svn client。物化器と repository.read/v1 Provider が同じ source を共有し、
    # 凭据解決と scope 裁剪を一箇所に閉じ込める (計画 §19 W4)。承認済み書き込み (§20) も
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
        secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
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
    # なく取得関数を渡し、解決を呼び出し時まで遅らせて組み立て順への依存を切る (計画 §23 P2)。
    engine_holder: dict[str, ClaudeAgentSdkEngine] = {}
    result_validator = ResultValidator(
        PostgresEvidenceLookup(ctx["database_session_factory"]),
        PostgresProposalLookup(ctx["database_session_factory"]),
        effect_lookup=PostgresEffectSummaryLookup(ctx["database_session_factory"]),
        artifact_lookup=PostgresArtifactLookup(ctx["database_session_factory"]),
    )
    registry = create_run_tool_registry(
        contracts,
        deferred_features_enabled=features.deferred,
        database_writes_enabled=features.database_writes,
        document_writes_enabled=features.document_writes,
        subagent_provider=SubagentDispatchProvider(
            engine=lambda: engine_holder["engine"],
            branch_timeout_seconds=settings.subagent_branch_timeout_seconds,
            session_recorder=PostgresSubagentSessionRecorder(ctx["database_session_factory"]),
            result_validator=result_validator,
        ) if settings.deferred_features_enabled else None,
        document_source=document_source,
        document_observations=PostgresDocumentObservationLookup(ctx["database_session_factory"]),
        document_readiness_provider=DocumentReadinessProvider(ctx["database_session_factory"]),
        mcp_provider=McpReadProvider(
            ctx["database_session_factory"], source=StreamableHttpMcpSource(),
            secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
        ),
        database_provider=DatabaseReadProvider(
            ctx["database_session_factory"], source=PostgresDatabaseSource(),
            secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
        ),
        redmine_issue_provider=RedmineIssueReadProvider(
            ctx["database_session_factory"],
            transport=UrllibRedmineTransport(),
            secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
        ),
        repository_source=repository_source,
    )
    def create_authorized_runtime(context: RunContext) -> RunMcpRuntime:
        """現在の Worker claim を一度だけ捕捉し、別 Run の audit writer を共有しない。"""

        authority = require_tool_authority(context)
        return registry.build_runtime(
            context,
            audit_writer=PostgresToolAuditWriter(
                ctx["database_session_factory"], claimed_run=authority.claimed,
                authority_check=authority.require_active,
            ),
        )

    runtime_configuration = ClaudeRuntimeConfiguration.from_environ()
    context_builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(settings.run_workspace_root),
        tool_registry=registry,
        model=runtime_configuration.primary_model,
        materializer=materializer,
        deferred_features_enabled=features.deferred,
        database_writes_enabled=features.database_writes,
        document_writes_enabled=features.document_writes,
        document_library_target=document_library_target,
    )
    engine = ClaudeAgentSdkEngine(
        mcp_server_factory=create_authorized_runtime,
        configuration=runtime_configuration,
        session_store=ctx["session_store"],
    )
    engine_holder["engine"] = engine
    ctx["run_executor"] = AgentRunExecutor(
        run_service=ctx["run_service"],
        context_builder=context_builder,
        engine=engine,
        result_validator=result_validator,
        lease_seconds=settings.run_lease_seconds,
        preparation_timeout_seconds=settings.run_preparation_timeout_seconds,
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
                ctx["database_session_factory"], features=features, target=document_library_target,
                limits=create_document_upload_limits(settings),
            )
        ctx["effect_executor"] = ApprovedEffectExecutor(
            effect_service=ctx["effect_service"],
            provider_registry=create_effect_provider_registry(
                features=features,
                effect_service=ctx["effect_service"],
                secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
                git_client=git_client,
                svn_client=svn_client,
                document_service=document_effect_service,
                document_source=document_effect_source,
            ),
            # 承認済み apply も MANAGED 凭据を使うため、読取 Provider と同じ KEK cipher を渡す。
            # 渡し漏れると「読めるのに承認後の書き込みだけ失敗する」非対称な障害になる。
            secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
            worker_id=ctx["worker_id"],
            lease_seconds=settings.run_lease_seconds,
            max_attempts=settings.run_max_attempts,
        )
    # Skill interpret を Worker 側で実行する。model への egress は Worker だけが持つ。
    interpreter, catalog, identity, default_model = build_skill_interpreter(settings)
    ctx["skill_service"] = SkillService(
        ctx["database_session_factory"],
        settings.contracts_dir,
        interpreter=interpreter,
        capability_catalog=catalog,
        interpreter_identity=identity,
        default_model=default_model,
        file_storage=create_file_storage(settings),
        storage_bucket=settings.object_storage_bucket,
    )
    ctx["interpret_publisher"] = RedisInterpretEventPublisher(cast(RedisPublisher, ctx["redis"]))
    # 調度の発火は即時実行と同じ RunService/SkillService を通す。別経路を作らないことが、
    # 承認・事前許可・binding 再検証が調度でだけ緩む事故を防ぐ唯一の方法 (計画 §22)。
    ctx["schedule_service"] = ScheduleService(
        ctx["database_session_factory"],
        skill_service=ctx["skill_service"],
        run_service=ctx["run_service"],
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Worker 終了時に Database connection pool を破棄する。"""

    engine: AsyncEngine = ctx["database_engine"]
    await engine.dispose()


async def worker_probe(_: dict[str, Any]) -> dict[str, str]:
    """Queue の疎通を決定的に確認する probe job を実行する。"""

    return {"status": "ok", "checked_at": datetime.now(UTC).isoformat()}


async def relay_outbox(ctx: dict[str, Any]) -> dict[str, int | str]:
    """未公開 Outbox message を ARQ Queue または Redis 通知へ配送する。"""

    settings = ctx["settings"]
    redis = cast(ArqRedis, ctx["redis"])
    relay: OutboxRelay = ctx["outbox_relay"]
    topics = {"run.lifecycle.changed/v1"}
    run_dispatch_ready = settings.worker_dispatch_enabled and "run_executor" in ctx
    effect_dispatch_ready = (
        settings.worker_dispatch_enabled
        and configured_execution_features(settings).effects_enabled
        and "effect_executor" in ctx
    )
    if run_dispatch_ready:
        topics.add("run.dispatch.requested/v1")
    if effect_dispatch_ready:
        topics.add("effect.apply.requested/v1")
    interpretation_dispatch_ready = settings.worker_dispatch_enabled and "skill_service" in ctx
    if interpretation_dispatch_ready:
        topics.add(INTERPRETATION_DISPATCH_TOPIC)
    reconciliation_dispatch_ready = (
        settings.worker_dispatch_enabled and "reconciliation_executor" in ctx
    )
    if reconciliation_dispatch_ready:
        topics.add(RECONCILIATION_DISPATCH_TOPIC)
    dispatch_ready = (
        run_dispatch_ready or effect_dispatch_ready or interpretation_dispatch_ready
        or reconciliation_dispatch_ready
    )

    async def publish(message: PendingOutboxMessage) -> None:
        """Topic ごとの外部配送を idempotent key 付きで実行する。"""

        if message.topic == RECONCILIATION_DISPATCH_TOPIC:
            await redis.enqueue_job(
                "execute_reconciliation_request_job", str(message.aggregate_id),
                _job_id=f"reconciliation-dispatch:{message.message_id}",
                _queue_name=settings.queue_name,
            )
            return
        if message.topic == INTERPRETATION_DISPATCH_TOPIC:
            await redis.enqueue_job(
                "execute_interpretation_request_job", str(message.aggregate_id),
                _job_id=f"interpret-dispatch:{message.message_id}",
                _queue_name=settings.queue_name,
            )
            return
        if message.topic == "run.dispatch.requested/v1":
            await redis.enqueue_job(
                "execute_run",
                str(message.aggregate_id),
                _job_id=f"run-dispatch:{message.message_id}",
                _queue_name=settings.queue_name,
            )
            return
        if message.topic == "run.lifecycle.changed/v1":
            await redis.publish(
                "skillmind:run-events",
                json.dumps(message.payload, ensure_ascii=False, separators=(",", ":")),
            )
            return
        if message.topic == "effect.apply.requested/v1":
            await redis.enqueue_job(
                "execute_effect",
                str(message.aggregate_id),
                _job_id=f"effect-dispatch:{message.message_id}",
                _queue_name=settings.queue_name,
            )
            return
        raise ValueError(f"Unsupported Outbox topic: {message.topic}")

    result = await relay.relay_once(topics=frozenset(topics), publisher=publish)
    log_event(
        logger,
        logging.INFO,
        "outbox.relay.completed",
        selected=result.selected,
        published=result.published,
        failed=result.failed,
    )
    return {
        "status": "ok",
        "selected": result.selected,
        "published": result.published,
        "failed": result.failed,
        "dispatch": "enabled" if dispatch_ready else "disabled",
    }


async def execute_run(ctx: dict[str, Any], run_id: str) -> dict[str, str | int]:
    """Queue job を idempotent に claim し、注入済み RunExecutor へ引き渡す。"""

    executor = cast(RunExecutor | None, ctx.get("run_executor"))
    if executor is None:
        # Feature gate の誤設定時も Run を claim する前に停止し、PREPARING 放置を防ぐ。
        raise RuntimeError("RunExecutor is not configured")

    service: RunService = ctx["run_service"]
    settings = ctx["settings"]
    if not settings.worker_dispatch_enabled:
        return {"status": "disabled", "reason": "run_dispatch_disabled"}
    claimed = await service.claim_run(
        UUID(run_id),
        worker_id=ctx["worker_id"],
        lease_seconds=settings.run_lease_seconds,
        max_attempts=settings.run_max_attempts,
    )
    if claimed is None:
        log_event(
            logger,
            logging.INFO,
            "run.execution.ignored",
            run_id=run_id,
            status="run_not_claimable",
            worker_id=ctx["worker_id"],
        )
        return {"status": "ignored", "reason": "run_not_claimable"}

    log_event(
        logger,
        logging.INFO,
        "run.execution.claimed",
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        attempt_no=claimed.attempt_no,
        worker_id=ctx["worker_id"],
        status="PREPARING",
    )
    try:
        await executor.execute(claimed)
    except Exception as error:
        # ARQ job 境界では再送判断のため例外を維持し、秘密を含まない型名だけを記録する。
        log_event(
            logger,
            logging.ERROR,
            "run.execution.failed",
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            attempt_no=claimed.attempt_no,
            worker_id=ctx["worker_id"],
            error_code=type(error).__name__,
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "run.execution.completed",
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        attempt_no=claimed.attempt_no,
        worker_id=ctx["worker_id"],
        status="terminalized",
    )
    return {
        "status": "accepted",
        "run_id": str(claimed.run_id),
        "run_attempt_id": str(claimed.run_attempt_id),
        "attempt_no": claimed.attempt_no,
    }


async def execute_effect(ctx: dict[str, Any], effect_execution_id: str) -> dict[str, str]:
    """Queue job を effect 専用 executor へ引き渡し、Agent Tool path と混在させない。"""

    settings: Settings = ctx["settings"]
    if (
        not settings.worker_dispatch_enabled
        or not configured_execution_features(settings).effects_enabled
    ):
        return {"status": "disabled", "reason": "effect_execution_disabled"}
    executor = cast(ApprovedEffectExecutor | None, ctx.get("effect_executor"))
    if executor is None:
        raise RuntimeError("EffectExecutor is not configured")
    status_value = await executor.execute(UUID(effect_execution_id))
    return {"status": status_value, "effect_execution_id": effect_execution_id}


def _interpret_progress(ctx: dict[str, Any], execution_key: str) -> InterpretProgressCallback:
    """Interpret 進行 event を execution key の channel へ配送する callback を作る。"""

    publisher: RedisInterpretEventPublisher = ctx["interpret_publisher"]

    async def on_event(event: str, data: Mapping[str, Any]) -> None:
        """解釈進行 event を当該 execution key の購読者へ中継する。"""

        await publisher.publish(execution_key=execution_key, event=event, data=data)

    return on_event


async def execute_interpretation_request_job(
    ctx: dict[str, Any], request_id: str,
) -> dict[str, str]:
    """Queue は原要求 ID だけを運び、入力・資格・通知先は持久要求から取得する。"""

    settings: Settings = ctx["settings"]
    if not settings.worker_dispatch_enabled:
        return {"status": "disabled", "reason": "worker_dispatch_disabled"}
    try:
        if not isinstance(request_id, str):
            raise ValueError("Invalid request ID")
        identity = UUID(request_id)
        if identity.int == 0:
            raise ValueError("Invalid request ID")
    except ValueError:
        return {"status": "rejected", "reason": "invalid_interpretation_request"}
    service: SkillService = ctx["skill_service"]
    try:
        stored = await service.execute_interpretation_request(
            identity, event_factory=lambda key: _interpret_progress(ctx, key)
        )
    except InterpretationRequestNotFoundError:
        return {"status": "rejected", "reason": "interpretation_request_not_found"}
    if stored is None:
        return {"status": "no_result", "request_id": str(identity)}
    # job の正常終了と解釈の成功は別の事実。保存済み終態を元要求へ対応付けて残す。
    log_event(
        logger, logging.INFO, "skill.interpret.request_completed",
        request_id=identity,
        execution_key=stored.execution_key,
        skill_source_id=stored.skill_source_id,
        interpretation_id=stored.interpretation_id,
        status=stored.status.value,
        error_code=stored.error_code,
    )
    return {"status": "ok", "interpretation_id": str(stored.interpretation_id)}


async def execute_reconciliation_request_job(
    ctx: dict[str, Any], request_id: str,
) -> dict[str, str]:
    """原要求 ID だけを executor に渡し、Queue の actor/接続/観測を信用しない。"""
    if not ctx["settings"].worker_dispatch_enabled:
        return {"status": "disabled", "reason": "worker_dispatch_disabled"}
    try:
        if not isinstance(request_id, str):
            raise ValueError("Invalid reconciliation request")
        identity = UUID(request_id)
        if identity.int == 0:
            raise ValueError("Invalid reconciliation request")
    except ValueError:
        return {"status": "rejected", "reason": "invalid_reconciliation_request"}
    executor = cast(EffectReconciliationExecutor | None, ctx.get("reconciliation_executor"))
    if executor is None:
        return {"status": "disabled", "reason": "reconciliation_unavailable"}
    try:
        status = await executor.execute(identity)
    except ReconciliationRequestNotFoundError:
        return {"status": "rejected", "reason": "reconciliation_request_not_found"}
    return {"status": status, "request_id": str(identity)}


async def recover_reconciliation_requests(ctx: dict[str, Any]) -> dict[str, int]:
    """旧 owner の期限を閉じるだけで、read/write の再 dispatch はしない。"""
    ledger: ReconciliationRequestService = ctx["reconciliation_requests"]
    return {"changed": await ledger.recover_expired(limit=ctx["settings"].outbox_batch_size)}


async def interpret_skill_source_job(
    ctx: dict[str, Any], kwargs: dict[str, Any],
) -> dict[str, str]:
    """旧 Queue に原会話を補造せず、新しい Worker では実行を明示拒否する。"""

    del ctx, kwargs
    return {"status": "rejected", "reason": "legacy_interpretation_job"}


async def adjust_skill_interpretation_job(
    ctx: dict[str, Any], kwargs: dict[str, Any],
) -> dict[str, str]:
    """旧調整 job も actor ID だけを認可として使わず、model を起動しない。"""

    del ctx, kwargs
    return {"status": "rejected", "reason": "legacy_interpretation_job"}


async def recover_interpretation_requests(ctx: dict[str, Any]) -> dict[str, int]:
    """停止事実を持たない古い RUNNING を UNKNOWN にし、再 dispatch しない。"""

    ledger = InterpretationRequestService(ctx["database_session_factory"])
    changed = await ledger.recover_unknown(
        before=datetime.now(UTC) - timedelta(seconds=1200),
        limit=ctx["settings"].outbox_batch_size,
    )
    return {"changed": changed}


async def recover_expired_leases(ctx: dict[str, Any]) -> dict[str, str | int]:
    """期限切れ lease、interaction、approval を回収し durable dispatch を作る。"""

    service: RunService = ctx["run_service"]
    effect_service: EffectService = ctx["effect_service"]
    settings = ctx["settings"]
    recovered_runs = await service.recover_expired_attempts(limit=settings.outbox_batch_size)
    recovered_interactions = await service.recover_expired_interactions(
        limit=settings.outbox_batch_size
    )
    recovered_effects = await effect_service.recover_expired_effects(
        limit=settings.outbox_batch_size,
        max_attempts=settings.run_max_attempts,
    ) if configured_execution_features(settings).effects_enabled else 0
    recovered_proposals = await effect_service.recover_expired_proposals(
        limit=settings.outbox_batch_size,
    ) if configured_execution_features(settings).effects_enabled else 0
    recovered = recovered_runs + recovered_interactions + recovered_effects + recovered_proposals
    log_event(
        logger,
        logging.WARNING if recovered else logging.INFO,
        "run.lease.recovery.completed",
        recovered=recovered,
        recovered_runs=recovered_runs,
        recovered_interactions=recovered_interactions,
        recovered_effects=recovered_effects,
        recovered_proposals=recovered_proposals,
        worker_id=ctx["worker_id"],
    )
    return {
        "status": "ok",
        "recovered": recovered,
        "recovered_runs": recovered_runs,
        "recovered_interactions": recovered_interactions,
        "recovered_effects": recovered_effects,
        "recovered_proposals": recovered_proposals,
    }


async def trigger_due_schedules(ctx: dict[str, Any]) -> dict[str, str | int]:
    """到期した TaskSchedule を認領して Run を作る (計画 §22)。

    tick 間隔より短い周期は表現できない。cron の最小粒度は分なので、毎分先頭付近で一度走れば
    取りこぼさない。二重発火は §22 D7 の決定的 idempotency key が防ぐため、複数 Worker が同時に
    走っても安全。
    """

    service: ScheduleService = ctx["schedule_service"]
    settings = ctx["settings"]
    if not settings.deferred_features_enabled or not settings.worker_dispatch_enabled:
        return {"status": "disabled", "reason": "scheduling_disabled"}
    report = await service.run_due_schedules(limit=settings.outbox_batch_size)
    log_event(
        logger,
        logging.WARNING if report.failed else logging.INFO,
        "schedule.tick.completed",
        created=report.created,
        skipped=report.skipped,
        failed=report.failed,
        worker_id=ctx["worker_id"],
    )
    return {
        "status": "ok",
        "created": report.created,
        "skipped": report.skipped,
        "failed": report.failed,
    }


_settings = get_settings()


class WorkerSettings:
    """M0 Worker を単一同時 job に制限する ARQ 設定。"""

    functions: ClassVar[tuple[Callable[..., Awaitable[Any]] | Function, ...]] = (
        worker_probe,
        # 資源準備を追加しても従来のモデル実行/終態化の余白を削らない。別 job は延長しない。
        arq_function(execute_run, timeout=1200 + _settings.run_preparation_timeout_seconds),
        execute_effect,
        execute_interpretation_request_job,
        execute_reconciliation_request_job,
        interpret_skill_source_job,
        adjust_skill_interpretation_job,
    )
    cron_jobs: ClassVar[tuple[CronJob, ...]] = (
        cron(
            recover_reconciliation_requests, second={11, 26, 41, 56},
            run_at_startup=True, unique=True, timeout=30,
        ),
        cron(
            recover_interpretation_requests, second={14, 29, 44, 59},
            run_at_startup=True, unique=True, timeout=30,
        ),
        cron(
            relay_outbox,
            second=set(range(0, 60, 5)),
            run_at_startup=True,
            unique=True,
            timeout=30,
        ),
        cron(
            recover_expired_leases,
            second={7, 22, 37, 52},
            run_at_startup=False,
            unique=True,
            timeout=30,
        ),
        # cron 式の最小粒度は分なので毎分一度で足りる。他の cron と秒をずらして DB 競合を避ける。
        cron(
            trigger_due_schedules,
            second={12},
            run_at_startup=False,
            unique=True,
            timeout=60,
        ),
    )
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    queue_name = _settings.queue_name
    max_jobs = 1
    # Run 自体の打ち切りは wall_timeout_seconds (900 秒) を Executor が強制する。
    # ARQ の job timeout はその graceful 終態化が完了する余白を持たせた最終防衛線とする。
    job_timeout = 1200
    keep_result = 3600
    health_check_interval = 30
