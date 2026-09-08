"""ARQ Worker の lifecycle、job、実行制約を定義する。"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, cast
from uuid import UUID

from arq import cron
from arq.connections import ArqRedis, RedisSettings
from arq.cron import CronJob
from arq.worker import Function
from arq.worker import func as arq_function
from sqlalchemy.ext.asyncio import AsyncEngine

from projectmind.agent.claude import ClaudeRuntimeConfiguration
from projectmind.agent.context_builder import (
    ContractStore,
    ProductionRunContextBuilder,
    create_run_tool_registry,
)
from projectmind.agent.engine import ClaudeAgentSdkEngine
from projectmind.agent.evidence import PostgresToolAuditWriter
from projectmind.agent.redmine_provider import RedmineIssueReadProvider
from projectmind.agent.repository_client import (
    GitCommandRepositoryClient,
    SvnCommandRepositoryClient,
)
from projectmind.agent.repository_source import IntegrationRepositorySnapshotSource
from projectmind.agent.result_validation import (
    PostgresEvidenceLookup,
    PostgresProposalLookup,
    ResultValidator,
)
from projectmind.agent.session_store import PostgresSessionStore
from projectmind.agent.subagent_provider import SubagentDispatchProvider
from projectmind.agent.subagent_sessions import PostgresSubagentSessionRecorder
from projectmind.agent.workspace import WorkspaceManager
from projectmind.agent.workspace_materializer import WorkspaceMaterializer
from projectmind.core.logging import configure_logging, log_event
from projectmind.core.secret_crypto import load_secret_cipher
from projectmind.core.settings import get_settings
from projectmind.db.resources import create_database_engine, create_session_factory
from projectmind.documents.source import (
    DatabaseProjectDocumentInventory,
    DatabaseProjectDocumentSource,
)
from projectmind.effects.forge import UrllibForgeTransport
from projectmind.effects.issue_update import (
    ISSUE_UPDATE_CAPABILITY,
    ISSUE_UPDATE_PROVIDER_VERSION,
)
from projectmind.effects.provider import (
    EffectProviderDefinition,
    EffectProviderRegistry,
)
from projectmind.effects.redmine import (
    UrllibRedmineTransport,
    create_redmine_effect_provider,
)
from projectmind.effects.repository_effect import (
    GitRepositoryWriteProvider,
    SvnRepositoryWriteProvider,
)
from projectmind.effects.repository_write import (
    REPOSITORY_WRITE_CAPABILITY,
    REPOSITORY_WRITE_PROVIDER_VERSION,
    REPOSITORY_WRITE_SVN_PROVIDER_VERSION,
)
from projectmind.effects.service import EffectService
from projectmind.integrations.secrets import DeploymentSecretResolver
from projectmind.runs.domain import PendingOutboxMessage
from projectmind.runs.outbox import OutboxRelay
from projectmind.runs.realtime import RedisPublisher, RedisRunRealtimePublisher
from projectmind.runs.repository_inputs import PostgresInputSnapshotStore
from projectmind.runs.service import RunService
from projectmind.schedules import ScheduleService
from projectmind.skills import (
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillInterpreterUnavailableError,
    SkillService,
    SkillSourceIntegrityError,
    SkillSourceNotFoundError,
    SkillStorageUnavailableError,
)
from projectmind.skills.interpreter_execution import InterpretProgressCallback
from projectmind.skills.realtime import RedisInterpretEventPublisher
from projectmind.skills.wiring import build_skill_interpreter
from projectmind.storage.factory import create_file_storage
from projectmind.worker.effects import ApprovedEffectExecutor
from projectmind.worker.executor import AgentRunExecutor, RunExecutor

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    """Worker process で共有する Database engine を初期化する。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    # MANAGED SecretReference 復号用の KEK cipher。未設定なら MANAGED は fail closed で解決不能。
    secret_cipher = load_secret_cipher(settings.managed_secret_kek)
    ctx["database_engine"] = create_database_engine(settings)
    ctx["database_session_factory"] = create_session_factory(ctx["database_engine"])
    ctx["session_store"] = PostgresSessionStore.from_session_factory(
        ctx["database_session_factory"]
    )
    ctx["run_service"] = RunService(ctx["database_session_factory"])
    ctx["effect_service"] = EffectService(ctx["database_session_factory"])
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
        file_storage=create_file_storage(settings),
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
    registry = create_run_tool_registry(
        contracts,
        subagent_provider=SubagentDispatchProvider(
            engine=lambda: engine_holder["engine"],
            branch_timeout_seconds=settings.subagent_branch_timeout_seconds,
            session_recorder=PostgresSubagentSessionRecorder(ctx["database_session_factory"]),
        ),
        document_source=document_source,
        redmine_issue_provider=RedmineIssueReadProvider(
            ctx["database_session_factory"],
            transport=UrllibRedmineTransport(),
            secret_resolver=DeploymentSecretResolver(cipher=secret_cipher),
        ),
        repository_source=repository_source,
    )
    audit_writer = PostgresToolAuditWriter(ctx["database_session_factory"])
    runtime_configuration = ClaudeRuntimeConfiguration.from_environ()
    context_builder = ProductionRunContextBuilder(
        workspace_manager=WorkspaceManager(settings.run_workspace_root),
        tool_registry=registry,
        model=runtime_configuration.primary_model,
        materializer=materializer,
    )
    engine = ClaudeAgentSdkEngine(
        mcp_server_factory=lambda context: registry.build_runtime(
            context, audit_writer=audit_writer
        ),
        configuration=runtime_configuration,
        session_store=ctx["session_store"],
    )
    engine_holder["engine"] = engine
    ctx["run_executor"] = AgentRunExecutor(
        run_service=ctx["run_service"],
        context_builder=context_builder,
        engine=engine,
        result_validator=ResultValidator(
            PostgresEvidenceLookup(ctx["database_session_factory"]),
            PostgresProposalLookup(ctx["database_session_factory"]),
        ),
        lease_seconds=settings.run_lease_seconds,
        preparation_timeout_seconds=settings.run_preparation_timeout_seconds,
        realtime_publisher=RedisRunRealtimePublisher(cast(RedisPublisher, ctx["redis"])),
    )
    ctx["effect_executor"] = ApprovedEffectExecutor(
        effect_service=ctx["effect_service"],
        provider_registry=EffectProviderRegistry(
            (
                EffectProviderDefinition(
                    capability_version=ISSUE_UPDATE_CAPABILITY,
                    provider="redmine",
                    provider_version=ISSUE_UPDATE_PROVIDER_VERSION,
                    implementation=create_redmine_effect_provider(),
                    requires_secret=True,
                ),
                EffectProviderDefinition(
                    capability_version=REPOSITORY_WRITE_CAPABILITY,
                    provider="svn",
                    provider_version=REPOSITORY_WRITE_SVN_PROVIDER_VERSION,
                    implementation=SvnRepositoryWriteProvider(svn_client),
                    requires_secret=True,
                ),
                EffectProviderDefinition(
                    capability_version=REPOSITORY_WRITE_CAPABILITY,
                    provider="git",
                    provider_version=REPOSITORY_WRITE_PROVIDER_VERSION,
                    implementation=GitRepositoryWriteProvider(
                        git_client, forge_transport=UrllibForgeTransport()
                    ),
                    # push は凭据必須。読取が匿名 clone で足りる (git の requires_secret=False)
                    # のと非対称なのは意図的で、判断は capability 粒度で持つ (計画 §20 R3)。
                    requires_secret=True,
                ),
            )
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
    effect_dispatch_ready = settings.worker_dispatch_enabled and "effect_executor" in ctx
    if run_dispatch_ready:
        topics.add("run.dispatch.requested/v1")
    if effect_dispatch_ready:
        topics.add("effect.apply.requested/v1")
    dispatch_ready = run_dispatch_ready or effect_dispatch_ready

    async def publish(message: PendingOutboxMessage) -> None:
        """Topic ごとの外部配送を idempotent key 付きで実行する。"""

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
                "projectmind:run-events",
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


async def interpret_skill_source_job(
    ctx: dict[str, Any],
    kwargs: dict[str, Any],
) -> dict[str, str]:
    """Worker 上で保存済み source を model 解釈し、進行を execution key channel へ流す。"""

    service: SkillService = ctx["skill_service"]
    execution_key = str(kwargs["execution_key"])
    try:
        stored = await service.interpret(
            organization_id=UUID(str(kwargs["organization_id"])),
            skill_source_id=UUID(str(kwargs["skill_source_id"])),
            model=kwargs.get("model"),
            parameters=kwargs.get("parameters") or {},
            force_regenerate=bool(kwargs.get("force_regenerate", False)),
            regeneration_nonce=(
                str(kwargs["regeneration_nonce"])
                if kwargs.get("regeneration_nonce") is not None
                else None
            ),
            on_event=_interpret_progress(ctx, execution_key),
        )
    except (
        SkillSourceNotFoundError,
        SkillInterpreterUnavailableError,
        SkillSourceIntegrityError,
        SkillStorageUnavailableError,
    ) as error:
        # 受理段階を通過済みのため通常は起きないが、job の可観測性のため型名だけ記録する。
        log_event(
            logger,
            logging.ERROR,
            "skill.interpret.job.failed",
            execution_key=execution_key,
            error_code=type(error).__name__,
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "skill.interpret.job.completed",
        execution_key=execution_key,
        interpretation_id=str(stored.interpretation_id),
        status=stored.status.value,
    )
    return {"status": "ok", "interpretation_id": str(stored.interpretation_id)}


async def adjust_skill_interpretation_job(
    ctx: dict[str, Any],
    kwargs: dict[str, Any],
) -> dict[str, str]:
    """Worker 上で親 interpretation に調整を適用し、進行を channel へ流す。"""

    service: SkillService = ctx["skill_service"]
    execution_key = str(kwargs["execution_key"])
    try:
        stored = await service.adjust_interpretation(
            organization_id=UUID(str(kwargs["organization_id"])),
            interpretation_id=UUID(str(kwargs["interpretation_id"])),
            instruction=str(kwargs["instruction"]),
            actor_id=UUID(str(kwargs["actor_id"])),
            model=kwargs.get("model"),
            parameters=kwargs.get("parameters") or {},
            on_event=_interpret_progress(ctx, execution_key),
        )
    except (
        SkillInterpretationNotFoundError,
        SkillInterpretationNotReadyError,
        SkillInterpreterUnavailableError,
        SkillSourceIntegrityError,
        SkillStorageUnavailableError,
    ) as error:
        log_event(
            logger,
            logging.ERROR,
            "skill.adjust.job.failed",
            execution_key=execution_key,
            error_code=type(error).__name__,
        )
        raise
    log_event(
        logger,
        logging.INFO,
        "skill.adjust.job.completed",
        execution_key=execution_key,
        interpretation_id=str(stored.interpretation_id),
        status=stored.status.value,
    )
    return {"status": "ok", "interpretation_id": str(stored.interpretation_id)}


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
    )
    recovered_proposals = await effect_service.recover_expired_proposals(
        limit=settings.outbox_batch_size,
    )
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
        interpret_skill_source_job,
        adjust_skill_interpretation_job,
    )
    cron_jobs: ClassVar[tuple[CronJob, ...]] = (
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
