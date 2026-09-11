"""PostgreSQL 上の Run、RunEvent、Outbox 永続化を実装する。"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    AgentSession,
    AgentTaskBriefSnapshot,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    InteractionResponse,
    OutboxMessage,
    Project,
    Run,
    RunAttempt,
    RunEvent,
    RunResult,
    RunSegment,
    RunSkillSnapshot,
    RuntimeManifest,
    SkillVersion,
    ToolCall,
    UserInteraction,
)
from skillmind.effects.domain import (
    ChangeProposalStatus,
    EffectExecutionStatus,
)
from skillmind.runs.budget import BudgetError
from skillmind.runs.creation_replay import validate_creation_replay
from skillmind.runs.creation_request import CREATION_REQUEST_FIELD, TaskRunIntent
from skillmind.runs.domain import (
    AgentSessionKind,
    AgentSessionMetadata,
    CancelledRun,
    ClaimedRun,
    ConcurrentRunUpdateError,
    CreatedRun,
    CreateRunCommand,
    IdempotencyConflictError,
    LeaseValidationError,
    PendingOutboxMessage,
    PreparedExecution,
    RunAttemptStatus,
    RunCancellationRequestedError,
    RunCancellationState,
    RunDetail,
    RunHistoryItem,
    RunHistoryPage,
    RunNotCancellableError,
    RunNotFoundError,
    RunResultRecord,
    RunSegmentStatus,
    RunSegmentTrigger,
    RunStatus,
    SessionContinuationMode,
    StoredAgentSession,
    StoredEvidence,
    StoredInteractionResponse,
    StoredRunAttempt,
    StoredRunEvent,
    StoredRunResult,
    StoredRunSegment,
    StoredRunSkillSnapshot,
    StoredToolCall,
    StoredUserInteraction,
    TaskLastRun,
    UserInteractionStatus,
    UserInteractionType,
    derive_task_id,
    plan_run_transition,
    request_hash,
)
from skillmind.runs.execution_outcome import user_cancellation_event
from skillmind.runs.repository_budgets import new_budget_account
from skillmind.runs.repository_effects import EffectOperationsMixin
from skillmind.runs.repository_interactions import InteractionOperationsMixin
from skillmind.skills.domain import PublishedTaskNotFoundError
from skillmind.skills.repository import SkillRepository


class RunRepository(InteractionOperationsMixin, EffectOperationsMixin):
    """一つの database transaction 内で Run aggregate を操作する。"""

    async def get_published_skill_binding(self, skill_version_id: UUID) -> dict[str, Any]:
        """Run snapshot 用に明示 version の frozen Manifest と checksum を取得する。"""

        version = await self._session.get(SkillVersion, skill_version_id)
        if version is None or version.status != "PUBLISHED":
            raise ValueError("Required SkillVersion is not published")
        manifest = (
            await self._session.scalars(
                select(RuntimeManifest).where(RuntimeManifest.skill_version_id == skill_version_id)
            )
        ).one_or_none()
        if manifest is None:
            raise ValueError("Published SkillVersion has no RuntimeManifest")
        return {
            "skill_version_id": str(version.id),
            "version": version.version,
            "manifest_checksum": manifest.checksum,
            "manifest": manifest.manifest_json,
        }

    async def create_idempotent(self, command: CreateRunCommand) -> CreatedRun:
        """三元 idempotency key で Run と初期 event/outbox を一度だけ作成する。"""

        now = datetime.now(UTC)
        run_id = uuid4()
        fingerprint = request_hash(command)
        statement = (
            insert(Run)
            .values(
                id=run_id,
                project_id=command.project_id,
                task_id=command.task_id,
                trigger_type="immediate",
                idempotency_key=command.idempotency_key,
                request_hash=fingerprint,
                status=RunStatus.QUEUED.value,
                input_json=command.input_json,
                task_snapshot_json=command.task_snapshot_json,
                permission_snapshot_json=command.permission_snapshot_json,
                selected_sources_json=command.selected_sources_json,
                limits_snapshot_json=command.limits_snapshot_json,
                row_version=1,
                started_at=None,
                finished_at=None,
                error_json=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_runs_project_task_idempotency")
            .returning(Run.id)
        )
        inserted_id = (await self._session.execute(statement)).scalar_one_or_none()

        if inserted_id is None:
            existing = await self._find_by_idempotency(command)
            if CREATION_REQUEST_FIELD in command.task_snapshot_json:
                validate_creation_replay(
                    existing,
                    TaskRunIntent.from_json(command.task_snapshot_json[CREATION_REQUEST_FIELD]),
                )
            elif existing.request_hash != fingerprint:
                raise IdempotencyConflictError(
                    "Idempotency-Key is already associated with a different request."
                )
            return self._to_created_run(existing, idempotent_replay=True)

        validated_skills = await self._validate_skill_snapshots(
            command.skill_snapshots_json, project_id=command.project_id
        )

        if command.budget_policy is not None:
            if command.limits_snapshot_json.get("budget_policy") != command.budget_policy.to_json():
                raise BudgetError("New Run requires its frozen budget policy marker")
            # INSERT 勝者の同じ transaction にだけ帳簿を追加する。新行の transient 表現を
            # 工場へ渡し、既存 Run の重放/復旧でゼロ帳簿を補造しない。
            new_run = Run(
                id=inserted_id,
                status=RunStatus.QUEUED.value,
                started_at=None,
                limits_snapshot_json=command.limits_snapshot_json,
            )
            self._session.add(new_budget_account(new_run, command.budget_policy))

        segment_id = uuid4()
        initial_segment = RunSegment(
            id=segment_id,
            run_id=run_id,
            segment_no=1,
            trigger_type=RunSegmentTrigger.INITIAL.value,
            trigger_ref=None,
            status=RunSegmentStatus.CREATED.value,
            objective_json={"source": "RUN_OBJECTIVE"},
            checkpoint_json={
                "summary": None,
                "confirmed_facts": [],
                "user_responses": [],
                "evidence_refs": [],
                "artifact_refs": [],
                "change_proposal_refs": [],
            },
            continuation_mode=SessionContinuationMode.INITIAL.value,
            parent_agent_session_id=None,
            instruction_snapshot_id=None,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )

        event = self._snapshot_event(
            run_id,
            sequence=1,
            payload={
                "status": RunStatus.QUEUED.value,
                "row_version": 1,
                "run_segment_id": str(segment_id),
                "segment_no": 1,
            },
            summary="Run queued",
            occurred_at=now,
            trace_id=command.trace_id,
        )
        dispatch = self._dispatch_outbox(
            run_id,
            payload={"run_id": str(run_id), "project_id": str(command.project_id)},
            occurred_at=now,
        )
        # Run、初期 event、dispatch request は同じ commit に含め、孤立 Run を防止する。
        skill_snapshots = [
            RunSkillSnapshot(
                id=uuid4(),
                run_id=run_id,
                skill_version_id=item["skill_version_id"],
                sort_order=item["sort_order"],
                config_snapshot_json=item["config_snapshot"],
                manifest_checksum=item["manifest_checksum"],
                created_at=now,
            )
            for item in validated_skills
        ]
        self._session.add_all([initial_segment, event, dispatch, *skill_snapshots])
        return CreatedRun(
            run_id=run_id,
            project_id=command.project_id,
            task_id=command.task_id,
            status=RunStatus.QUEUED,
            row_version=1,
            created_at=now,
            idempotent_replay=False,
        )

    async def find_task_run_replay(
        self, *, intent: TaskRunIntent, idempotency_key: str
    ) -> CreatedRun | None:
        """現在の資源や公開状態を調べる前に、初回 commit 済みの同一要求を確認する。"""

        existing = await self._load_by_idempotency(
            project_id=intent.project_id,
            task_id=derive_task_id(
                skill_version_id=intent.skill_version_id, task_key=intent.task_key
            ),
            idempotency_key=idempotency_key,
        )
        if existing is None:
            return None
        validate_creation_replay(existing, intent)
        return self._to_created_run(existing, idempotent_replay=True)

    async def get(self, run_id: UUID) -> CreatedRun:
        """指定 ID の Run を取得し、存在しなければ domain error を返す。"""

        run = await self._session.get(Run, run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        return self._to_created_run(run, idempotent_replay=False)

    async def replace_initial_selected_sources(
        self, *, run_id: UUID, selected_sources: dict[str, Any]
    ) -> None:
        """初期作成 transaction 内で Run binding ID を source snapshot へ反映する。

        この method は dispatch commit 前だけに呼ぶ。実行開始後の snapshot 更新を許すもの
        ではなく、`create_idempotent` が生成した Run と同じ transaction で ResourceBinding 行の
        UUID を結ぶための初期化手順である。
        """

        run = await self._session.get(Run, run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        if run.status != RunStatus.QUEUED.value or run.row_version != 1:
            raise ConcurrentRunUpdateError("Run source snapshot is already immutable")
        run.selected_sources_json = selected_sources

    async def get_detail(self, *, project_id: UUID, run_id: UUID) -> RunDetail:
        """Project と Run の複合条件を満たす Result、ToolCall、Evidence を取得する。"""

        run = (
            await self._session.scalars(
                select(Run).where(Run.id == run_id, Run.project_id == project_id)
            )
        ).one_or_none()
        if run is None:
            # 存在有無を Project 外へ漏らさず、ownership 不一致も同じ not found に畳み込む。
            raise RunNotFoundError(f"Run not found in project: {run_id}")

        result = (
            await self._session.scalars(select(RunResult).where(RunResult.run_id == run_id))
        ).one_or_none()
        tool_calls = (
            await self._session.scalars(
                select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)
            )
        ).all()
        evidence = (
            await self._session.scalars(
                select(Evidence)
                .join(ToolCall, Evidence.tool_call_id == ToolCall.id)
                .where(Evidence.run_id == run_id, ToolCall.run_id == run_id)
                .order_by(Evidence.created_at)
            )
        ).all()
        skill_snapshots = (
            await self._session.scalars(
                select(RunSkillSnapshot)
                .where(RunSkillSnapshot.run_id == run_id)
                .order_by(RunSkillSnapshot.sort_order)
            )
        ).all()
        segments = (
            await self._session.scalars(
                select(RunSegment)
                .where(RunSegment.run_id == run_id)
                .order_by(RunSegment.segment_no)
            )
        ).all()
        attempts = (
            await self._session.scalars(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.created_at, RunAttempt.id)
            )
        ).all()
        sessions = (
            await self._session.scalars(
                select(AgentSession)
                .where(AgentSession.run_id == run_id)
                .order_by(AgentSession.created_at, AgentSession.id)
            )
        ).all()
        interactions = (
            await self._session.scalars(
                select(UserInteraction)
                .where(UserInteraction.run_id == run_id)
                .order_by(UserInteraction.created_at, UserInteraction.id)
            )
        ).all()
        responses = (
            await self._session.scalars(
                select(InteractionResponse)
                .where(InteractionResponse.run_id == run_id)
                .order_by(InteractionResponse.created_at, InteractionResponse.id)
            )
        ).all()
        proposals = (
            await self._session.scalars(
                select(ChangeProposal)
                .where(ChangeProposal.run_id == run_id)
                .order_by(ChangeProposal.created_at, ChangeProposal.id)
            )
        ).all()
        approvals = (
            await self._session.scalars(
                select(ChangeApproval)
                .where(ChangeApproval.run_id == run_id)
                .order_by(ChangeApproval.created_at, ChangeApproval.id)
            )
        ).all()
        effect_executions = (
            await self._session.scalars(
                select(EffectExecution)
                .where(EffectExecution.run_id == run_id)
                .order_by(EffectExecution.created_at, EffectExecution.id)
            )
        ).all()
        briefs = (
            await self._session.scalars(
                select(AgentTaskBriefSnapshot).where(AgentTaskBriefSnapshot.run_id == run_id)
            )
        ).all()
        brief_by_segment = {item.run_segment_id: item for item in briefs}
        response_by_interaction = {item.interaction_id: item for item in responses}
        projected_segments = tuple(
            StoredRunSegment(
                run_segment_id=item.id,
                segment_no=item.segment_no,
                trigger_type=RunSegmentTrigger(item.trigger_type),
                trigger_ref=item.trigger_ref,
                status=RunSegmentStatus(item.status),
                objective=dict(item.objective_json),
                checkpoint=dict(item.checkpoint_json),
                continuation_mode=SessionContinuationMode(item.continuation_mode),
                parent_agent_session_id=item.parent_agent_session_id,
                task_brief_checksum=(
                    brief_by_segment[item.id].checksum if item.id in brief_by_segment else None
                ),
                started_at=item.started_at,
                finished_at=item.finished_at,
                created_at=item.created_at,
            )
            for item in segments
        )
        if not projected_segments:
            # Release G 前の Run は row を捏造せず、read model だけで implicit Segment 1 とする。
            projected_segments = (_implicit_segment(run),)
        return RunDetail(
            run=self._to_created_run(run, idempotent_replay=False),
            input=dict(run.input_json),
            selected_sources=dict(run.selected_sources_json),
            output_schema=(
                dict(run.task_snapshot_json["output_schema_json"])
                if isinstance(run.task_snapshot_json.get("output_schema_json"), dict)
                else None
            ),
            output_schema_checksum=(
                str(run.task_snapshot_json["output_schema_checksum"])
                if isinstance(run.task_snapshot_json.get("output_schema_checksum"), str)
                else None
            ),
            result=None if result is None else self._to_stored_result(result),
            tool_calls=tuple(
                StoredToolCall(
                    tool_call_id=item.id,
                    run_attempt_id=item.run_attempt_id,
                    agent_session_id=item.agent_session_id,
                    tool_name=item.tool_name,
                    capability=item.capability_version,
                    provider=item.provider,
                    arguments_summary=dict(item.arguments_summary),
                    status=item.status,
                    duration_ms=item.duration_ms,
                    created_at=item.created_at,
                )
                for item in tool_calls
            ),
            evidence=tuple(
                StoredEvidence(
                    evidence_ref=item.evidence_ref,
                    tool_call_id=item.tool_call_id,
                    evidence_type=item.evidence_type,
                    source_uri=item.source_uri,
                    source_locator=dict(item.source_locator),
                    content_hash=item.content_hash,
                    snapshot_uri=item.snapshot_uri,
                    excerpt=item.excerpt,
                    metadata=dict(item.metadata_json),
                    created_at=item.created_at,
                )
                for item in evidence
            ),
            skill_snapshots=tuple(
                StoredRunSkillSnapshot(
                    skill_version_id=item.skill_version_id,
                    sort_order=item.sort_order,
                    manifest_checksum=item.manifest_checksum,
                    config_snapshot=dict(item.config_snapshot_json),
                )
                for item in skill_snapshots
            ),
            segments=projected_segments,
            attempts=tuple(
                StoredRunAttempt(
                    run_attempt_id=item.id,
                    run_segment_id=item.run_segment_id,
                    attempt_no=item.attempt_no,
                    reason=item.reason,
                    status=RunAttemptStatus(item.status),
                    worker_id=item.worker_id,
                    started_at=item.started_at,
                    finished_at=item.finished_at,
                    error=dict(item.error_json) if item.error_json is not None else None,
                    created_at=item.created_at,
                )
                for item in attempts
            ),
            sessions=tuple(
                StoredAgentSession(
                    agent_session_id=item.id,
                    run_segment_id=item.run_segment_id,
                    run_attempt_id=item.run_attempt_id,
                    sdk_session_id=item.sdk_session_id,
                    parent_session_id=item.parent_session_id,
                    continuation_mode=SessionContinuationMode(item.continuation_mode),
                    session_kind=AgentSessionKind(item.session_kind),
                    checkpoint_checksum=item.checkpoint_checksum,
                    engine_options_checksum=item.engine_options_checksum,
                    engine=item.engine,
                    sdk_version=item.sdk_version,
                    cli_version=item.cli_version,
                    model=item.model,
                    status=item.status,
                    usage=dict(item.usage_json),
                    cost=dict(item.cost_json),
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in sessions
            ),
            interactions=tuple(
                _to_stored_interaction(
                    item,
                    response_by_interaction.get(item.id),
                )
                for item in interactions
            ),
            change_proposals=tuple(self._stored_change_proposal(item) for item in proposals),
            approvals=tuple(self._stored_change_approval(item) for item in approvals),
            effect_executions=tuple(
                self._stored_effect_execution(item) for item in effect_executions
            ),
        )

    async def latest_run_by_task(self, *, project_id: UUID) -> dict[UUID, TaskLastRun]:
        """Project 内の各 task について最新 Run を一件だけ返す。

        window 関数で task ごとに 1 行へ絞る。画面側が「直近 N 件」を引いて突き合わせる実装だと、
        N 件より古い task が「未実行」と表示され、欠落ではなく**誤った値**が並ぶ。件数上限に
        依存しない形はこの一手しかない。
        """

        ranked = (
            select(
                Run.id.label("run_id"),
                Run.task_id.label("task_id"),
                Run.status.label("status"),
                Run.created_at.label("created_at"),
                Run.finished_at.label("finished_at"),
                RunResult.summary.label("summary"),
                func.row_number()
                .over(
                    partition_by=Run.task_id,
                    order_by=(Run.created_at.desc(), Run.id.desc()),
                )
                .label("rank"),
            )
            .outerjoin(RunResult, RunResult.run_id == Run.id)
            .where(Run.project_id == project_id)
            .subquery()
        )
        rows = (await self._session.execute(select(ranked).where(ranked.c.rank == 1))).all()
        return {
            row.task_id: TaskLastRun(
                task_id=row.task_id,
                run_id=row.run_id,
                status=RunStatus(row.status),
                created_at=row.created_at,
                finished_at=row.finished_at,
                result_summary=row.summary,
            )
            for row in rows
        }

    async def list_history(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        statuses: tuple[RunStatus, ...] = (),
    ) -> RunHistoryPage:
        """Project 内の Run と任意 Result summary を新しい順にページ取得する。

        `statuses` を渡すとその状態だけに絞る。「回答待ち・承認待ちの Run を全部出す」は
        画面側で先頭 page を filter しても作れないため (待機中の Run が古い page にあると
        取りこぼす)、絞り込みは SQL 側で行う。
        """

        statement = (
            select(Run, RunResult)
            .outerjoin(RunResult, RunResult.run_id == Run.id)
            .where(Run.project_id == project_id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
        if statuses:
            statement = statement.where(Run.status.in_([status.value for status in statuses]))
        rows = (await self._session.execute(statement)).all()
        has_more = len(rows) > limit
        return RunHistoryPage(
            items=tuple(
                RunHistoryItem(
                    run=self._to_created_run(run, idempotent_replay=False),
                    input=dict(run.input_json),
                    selected_sources=dict(run.selected_sources_json),
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    result_summary=result.summary if result is not None else None,
                    result_confidence=result.confidence if result is not None else None,
                    result_needs_review=result.needs_review if result is not None else None,
                )
                for run, result in rows[:limit]
            ),
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    async def list_events(
        self, run_id: UUID, *, after: int, limit: int = 100
    ) -> list[StoredRunEvent]:
        """RunEvent を sequence 昇順で replay 用に取得する。"""

        statement = (
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
            .order_by(RunEvent.sequence)
            .limit(limit)
        )
        events = (await self._session.scalars(statement)).all()
        return [
            StoredRunEvent(
                run_id=event.run_id,
                run_attempt_id=event.run_attempt_id,
                agent_session_id=event.agent_session_id,
                sequence=event.sequence,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                payload=event.payload_json,
                trace_id=event.trace_id,
            )
            for event in events
        ]

    async def transition(
        self,
        run_id: UUID,
        *,
        target: RunStatus,
        expected_row_version: int,
        error_json: dict[str, Any] | None = None,
        run_attempt_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> CreatedRun:
        """Row lock と version check の下で Run 状態と event/outbox を更新する。"""

        run = await self._lock_run_row(run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        if run.row_version != expected_row_version:
            raise ConcurrentRunUpdateError(
                "Run row version changed: "
                f"expected={expected_row_version}, actual={run.row_version}"
            )

        now = datetime.now(UTC)
        transition = plan_run_transition(
            current=RunStatus(run.status),
            target=target,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.started_at = transition.started_at
        run.finished_at = transition.finished_at
        run.error_json = error_json
        run.updated_at = now

        sequence = await self._next_sequence(run_id)
        event = self._snapshot_event(
            run_id,
            sequence=sequence,
            payload={
                "status": transition.status.value,
                "row_version": transition.row_version,
                "error": error_json,
            },
            summary=f"Run transitioned to {transition.status.value}",
            occurred_at=now,
            run_attempt_id=run_attempt_id,
            trace_id=trace_id,
        )
        self._session.add_all([event, self._event_outbox(event, status=transition.status.value)])
        return self._to_created_run(run, idempotent_replay=False)

    async def request_cancellation(
        self,
        run_id: UUID,
        *,
        trace_id: str | None = None,
    ) -> CancelledRun:
        """Run を lock し、実行中なら取消 intent、未実行なら取消終態を記録する。"""

        run = await self._lock_run_row(run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")

        current = RunStatus(run.status)
        if current is RunStatus.CANCELLED:
            return CancelledRun(
                run=self._to_created_run(run, idempotent_replay=False),
                cancellation=RunCancellationState.CANCELLED,
            )
        if current in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            raise RunNotCancellableError(f"Terminal Run cannot be cancelled: {current.value}")

        active_effect = None
        if current is RunStatus.WAITING_FOR_APPROVAL:
            active_effect = (
                await self._session.scalars(
                    select(EffectExecution)
                    .where(
                        EffectExecution.run_id == run_id,
                        EffectExecution.status.in_(
                            {
                                EffectExecutionStatus.LEASED.value,
                                EffectExecutionStatus.APPLYING.value,
                            }
                        ),
                    )
                    .order_by(EffectExecution.created_at.desc())
                    .limit(1)
                )
            ).one_or_none()
        if current in {RunStatus.PREPARING, RunStatus.RUNNING} or active_effect is not None:
            existing = await self._session.scalar(
                select(RunEvent.id).where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == "RUN_CANCEL_REQUESTED",
                )
            )
            if existing is None:
                now = datetime.now(UTC)
                active_attempt_id = await self._session.scalar(
                    select(RunAttempt.id)
                    .where(
                        RunAttempt.run_id == run_id,
                        RunAttempt.status.in_(
                            {RunAttemptStatus.LEASED.value, RunAttemptStatus.RUNNING.value}
                        ),
                    )
                    .order_by(RunAttempt.attempt_no.desc())
                    .limit(1)
                )
                if active_attempt_id is None and active_effect is not None:
                    proposal = await self._session.get(ChangeProposal, active_effect.proposal_id)
                    active_attempt_id = proposal.run_attempt_id if proposal is not None else None
                event = RunEvent(
                    id=uuid4(),
                    run_id=run_id,
                    run_attempt_id=active_attempt_id,
                    agent_session_id=None,
                    sequence=await self._next_sequence(run_id),
                    event_type="RUN_CANCEL_REQUESTED",
                    payload_json={"status": current.value},
                    occurred_at=now,
                    trace_id=trace_id,
                    summary="Run cancellation requested",
                )
                self._session.add_all([event, self._event_outbox(event, status=current.value)])
            return CancelledRun(
                run=self._to_created_run(run, idempotent_replay=False),
                cancellation=RunCancellationState.REQUESTED,
            )

        now = datetime.now(UTC)
        transition = plan_run_transition(
            current=current,
            target=RunStatus.CANCELLED,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.finished_at = transition.finished_at
        run.error_json = None
        run.updated_at = now

        if current in {
            RunStatus.WAITING_PERMISSION,
            RunStatus.WAITING_FOR_INPUT,
            RunStatus.WAITING_FOR_APPROVAL,
        }:
            segment = (
                await self._session.scalars(
                    select(RunSegment)
                    .where(RunSegment.run_id == run_id)
                    .order_by(RunSegment.segment_no.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).one_or_none()
            attempt = await self._session.scalar(
                select(RunAttempt)
                .where(
                    RunAttempt.run_id == run_id,
                    *((RunAttempt.run_segment_id == segment.id,) if segment is not None else ()),
                    RunAttempt.status == RunAttemptStatus.DEFERRED.value,
                )
                .order_by(RunAttempt.attempt_no.desc())
                .limit(1)
                .with_for_update()
            )
            if attempt is not None:
                attempt.status = RunAttemptStatus.CANCELLED.value
                attempt.finished_at = now
                attempt.updated_at = now
            if segment is not None:
                segment.status = RunSegmentStatus.CANCELLED.value
                segment.finished_at = now
                segment.updated_at = now
            open_interactions = (
                await self._session.scalars(
                    select(UserInteraction)
                    .where(
                        UserInteraction.run_id == run_id,
                        UserInteraction.status == UserInteractionStatus.OPEN.value,
                    )
                    .with_for_update()
                )
            ).all()
            for interaction in open_interactions:
                interaction.status = UserInteractionStatus.CANCELLED.value
                interaction.updated_at = now
            requested_effects = (
                await self._session.scalars(
                    select(EffectExecution)
                    .where(
                        EffectExecution.run_id == run_id,
                        EffectExecution.status == EffectExecutionStatus.REQUESTED.value,
                    )
                    .with_for_update()
                )
            ).all()
            for effect in requested_effects:
                effect.status = EffectExecutionStatus.FAILED.value
                effect.error_json = {"code": "run_cancelled", "retryable": False}
                effect.updated_at = now
                proposal = await self._session.get(ChangeProposal, effect.proposal_id)
                if proposal is not None:
                    proposal.status = ChangeProposalStatus.FAILED.value
                    proposal.updated_at = now

        if current in {RunStatus.QUEUED, RunStatus.RETRY_PENDING}:
            queued_segment = (
                await self._session.scalars(
                    select(RunSegment)
                    .where(RunSegment.run_id == run_id)
                    .order_by(RunSegment.segment_no.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).one_or_none()
            if queued_segment is not None:
                queued_segment.status = RunSegmentStatus.CANCELLED.value
                queued_segment.finished_at = now
                queued_segment.updated_at = now

        event = self._snapshot_event(
            run_id,
            sequence=await self._next_sequence(run_id),
            payload={
                "status": RunStatus.CANCELLED.value,
                "row_version": transition.row_version,
                "error": None,
            },
            summary="Run cancelled before active execution",
            occurred_at=now,
            trace_id=trace_id,
        )
        self._session.add_all([event, self._event_outbox(event, status=RunStatus.CANCELLED.value)])
        return CancelledRun(
            run=self._to_created_run(run, idempotent_replay=False),
            cancellation=RunCancellationState.CANCELLED,
        )

    async def claim_for_execution(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        lease_token: str,
        lease_token_hash: str,
        lease_expires_at: datetime,
        max_attempts: int,
        trace_id: str | None = None,
    ) -> ClaimedRun | None:
        """Queued/Retry Run を lock し、新しい RunAttempt と lease を原子的に作成する。

        再試行回数が max_attempts を超える Run は claim せず、FAILED へ終態化する。
        """

        run = await self._lock_run_row(run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        current = RunStatus(run.status)
        if current not in {RunStatus.QUEUED, RunStatus.RETRY_PENDING}:
            # 重複 Queue job は既に進行した Run を再度 claim せず、idempotent no-op とする。
            return None

        segment = (
            await self._session.scalars(
                select(RunSegment)
                .where(RunSegment.run_id == run_id)
                .order_by(RunSegment.segment_no.desc())
                .limit(1)
                .with_for_update()
            )
        ).one_or_none()
        if segment is None:
            # 歴史 Run は read-only implicit Segment として表示できるが、新しい Worker claim に
            # 暗黙 row を生成すると作成理由を捏造するため fail closed にする。
            raise LeaseValidationError("Queued Run has no explicit RunSegment")
        if RunSegmentStatus(segment.status) not in {
            RunSegmentStatus.CREATED,
            RunSegmentStatus.RUNNING,
        }:
            return None

        active_statement = select(RunAttempt.id).where(
            RunAttempt.run_id == run_id,
            RunAttempt.run_segment_id == segment.id,
            RunAttempt.status.in_(
                {
                    RunAttemptStatus.CREATED.value,
                    RunAttemptStatus.LEASED.value,
                    RunAttemptStatus.RUNNING.value,
                    RunAttemptStatus.DEFERRED.value,
                }
            ),
        )
        if (await self._session.scalar(active_statement)) is not None:
            return None

        attempt_no_statement = select(func.coalesce(func.max(RunAttempt.attempt_no), 0) + 1).where(
            RunAttempt.run_segment_id == segment.id
        )
        attempt_no = int((await self._session.scalar(attempt_no_statement)) or 1)
        now = datetime.now(UTC)
        if attempt_no > max_attempts:
            # QUEUED は attempt 0 件で上限に達しないため、この分岐は RETRY_PENDING だけが通る。
            await self._exhaust_retries(
                run,
                segment,
                attempt_no=attempt_no,
                max_attempts=max_attempts,
                now=now,
                trace_id=trace_id,
            )
            return None
        attempt_id = uuid4()
        attempt = RunAttempt(
            id=attempt_id,
            run_id=run_id,
            run_segment_id=segment.id,
            attempt_no=attempt_no,
            reason=(
                "INITIAL"
                if segment.segment_no == 1 and attempt_no == 1
                else "CONTINUATION"
                if attempt_no == 1
                else "RETRY"
            ),
            status=RunAttemptStatus.LEASED.value,
            worker_id=worker_id,
            lease_token_hash=lease_token_hash,
            lease_expires_at=lease_expires_at,
            heartbeat_at=now,
            started_at=now,
            finished_at=None,
            error_json=None,
            created_at=now,
            updated_at=now,
        )
        transition = plan_run_transition(
            current=current,
            target=RunStatus.PREPARING,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.started_at = transition.started_at
        run.finished_at = transition.finished_at
        run.updated_at = now

        segment_was_created = RunSegmentStatus(segment.status) is RunSegmentStatus.CREATED
        segment.status = RunSegmentStatus.RUNNING.value
        segment.started_at = segment.started_at or now
        segment.updated_at = now

        next_sequence = await self._next_sequence(run_id)
        stored: list[Any] = []
        if segment_was_created:
            segment_event = RunEvent(
                id=uuid4(),
                run_id=run_id,
                run_attempt_id=attempt_id,
                agent_session_id=None,
                sequence=next_sequence,
                event_type=AgentEventType.SEGMENT_STARTED.value,
                payload_json={
                    "run_segment_id": str(segment.id),
                    "segment_no": segment.segment_no,
                    "trigger_type": segment.trigger_type,
                },
                occurred_at=now,
                trace_id=trace_id,
                summary="Run segment started",
            )
            stored.extend(
                [segment_event, self._event_outbox(segment_event, status=RunStatus.PREPARING.value)]
            )
            next_sequence += 1
        event = self._snapshot_event(
            run_id,
            sequence=next_sequence,
            payload={
                "status": RunStatus.PREPARING.value,
                "row_version": transition.row_version,
                "attempt_no": attempt_no,
                "run_segment_id": str(segment.id),
                "segment_no": segment.segment_no,
            },
            summary="Run claimed by Worker",
            occurred_at=now,
            run_attempt_id=attempt_id,
            trace_id=trace_id,
        )
        stored.extend([attempt, event, self._event_outbox(event, status=RunStatus.PREPARING.value)])
        self._session.add_all(stored)
        parent_session = None
        if segment.parent_agent_session_id is not None:
            parent_session = await self._session.get(AgentSession, segment.parent_agent_session_id)
            if parent_session is None or parent_session.run_id != run_id:
                raise LeaseValidationError("RunSegment parent AgentSession is invalid")
        checkpoint = dict(segment.checkpoint_json)
        checkpoint_checksum = (
            f"sha256:{sha256_hex(canonical_json(checkpoint))}" if segment.segment_no > 1 else None
        )
        return ClaimedRun(
            run_id=run_id,
            run_attempt_id=attempt_id,
            project_id=run.project_id,
            actor_id=UUID(str(run.permission_snapshot_json["actor_id"])),
            attempt_no=attempt_no,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            row_version=transition.row_version,
            input_json=run.input_json,
            task_snapshot_json=run.task_snapshot_json,
            permission_snapshot_json=run.permission_snapshot_json,
            selected_sources_json=run.selected_sources_json,
            limits_snapshot_json=run.limits_snapshot_json,
            skill_snapshots_json=tuple(run.task_snapshot_json.get("skill_snapshots", ())),
            run_segment_id=segment.id,
            segment_no=segment.segment_no,
            continuation_mode=SessionContinuationMode(segment.continuation_mode),
            parent_agent_session_id=segment.parent_agent_session_id,
            parent_sdk_session_id=(
                parent_session.sdk_session_id if parent_session is not None else None
            ),
            parent_run_attempt_id=(
                parent_session.run_attempt_id if parent_session is not None else None
            ),
            checkpoint_json=checkpoint,
            checkpoint_checksum=checkpoint_checksum,
            segment_objective_json=dict(segment.objective_json),
        )

    async def _validate_skill_snapshots(
        self, snapshots: tuple[dict[str, Any], ...], *, project_id: UUID
    ) -> tuple[dict[str, Any], ...]:
        """新規 INSERT のみ、現在の有効化と frozen Manifest checksum を同じ TX で固定する。"""

        validated: list[dict[str, Any]] = []
        if not snapshots:
            return ()
        # 上位 use case が資格と Project を固定済み。原 Run の重放には今日の可用性を課さない。
        organization_id = await self._session.scalar(
            select(Project.organization_id).where(Project.id == project_id)
        )
        if not isinstance(organization_id, UUID):
            raise PublishedTaskNotFoundError("Published task is not available")
        for snapshot in snapshots:
            try:
                skill_version_id = UUID(str(snapshot["skill_version_id"]))
                sort_order = int(snapshot["sort_order"])
                manifest_checksum = str(snapshot["manifest_checksum"])
                manifest_payload = snapshot["manifest"]
                config_snapshot = snapshot.get("config_snapshot", {})
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Run skill snapshot is invalid") from error
            await SkillRepository(self._session).require_current_task_binding(
                organization_id=organization_id,
                project_id=project_id,
                skill_version_id=skill_version_id,
            )
            manifest = (
                await self._session.scalars(
                    select(RuntimeManifest).where(
                        RuntimeManifest.skill_version_id == skill_version_id
                    )
                )
            ).one_or_none()
            actual_checksum = (
                f"sha256:{sha256_hex(canonical_json(manifest_payload))}"
                if isinstance(manifest_payload, dict)
                else ""
            )
            if (
                manifest is None
                or manifest.checksum != manifest_checksum
                or actual_checksum != manifest_checksum
                or canonical_json(manifest.manifest_json) != canonical_json(manifest_payload)
            ):
                raise ValueError("Run SkillVersion Manifest checksum does not match")
            if not isinstance(config_snapshot, dict):
                raise ValueError("Run skill config snapshot must be an object")
            validated.append(
                {
                    "skill_version_id": skill_version_id,
                    "sort_order": sort_order,
                    "manifest_checksum": manifest_checksum,
                    "config_snapshot": config_snapshot,
                }
            )
        return tuple(validated)

    async def _exhaust_retries(
        self,
        run: Run,
        segment: RunSegment,
        *,
        attempt_no: int,
        max_attempts: int,
        now: datetime,
        trace_id: str | None,
    ) -> None:
        """再試行上限に達した Run を再 claim させず FAILED へ終態化する。"""

        error_json = {
            "code": "retry_exhausted",
            "retryable": False,
            "attempts": attempt_no - 1,
            "max_attempts": max_attempts,
        }
        transition = plan_run_transition(
            current=RunStatus(run.status),
            target=RunStatus.FAILED,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.started_at = transition.started_at
        run.finished_at = transition.finished_at
        run.error_json = error_json
        run.updated_at = now
        segment.status = RunSegmentStatus.FAILED.value
        segment.finished_at = now
        segment.updated_at = now
        event = self._snapshot_event(
            run.id,
            sequence=await self._next_sequence(run.id),
            payload={
                "status": RunStatus.FAILED.value,
                "row_version": transition.row_version,
                "error": error_json,
            },
            summary="Run retry limit exhausted",
            occurred_at=now,
            trace_id=trace_id,
        )
        self._session.add_all([event, self._event_outbox(event, status=RunStatus.FAILED.value)])

    async def prepare_execution(self, claimed: ClaimedRun) -> PreparedExecution:
        """Lease を再検証し、Attempt と Run を RUNNING へ原子的に進める。"""

        run, segment, attempt = await self._lock_claimed_execution(claimed)
        now = datetime.now(UTC)
        self._validate_claimed_lease(attempt, claimed, now=now)
        if RunStatus(run.status) is not RunStatus.PREPARING:
            raise LeaseValidationError(f"Run is not preparing: {run.status}")
        if run.row_version != claimed.row_version:
            raise ConcurrentRunUpdateError(
                f"Run row version changed before execution: {run.row_version}"
            )

        transition = plan_run_transition(
            current=RunStatus.PREPARING,
            target=RunStatus.RUNNING,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.started_at = transition.started_at
        run.updated_at = now
        attempt.status = RunAttemptStatus.RUNNING.value
        attempt.updated_at = now
        if segment is not None:
            segment.status = RunSegmentStatus.RUNNING.value
            segment.started_at = segment.started_at or now
            segment.updated_at = now

        sequence = await self._next_sequence(run.id)
        event = self._snapshot_event(
            run.id,
            sequence=sequence,
            payload={
                "status": RunStatus.RUNNING.value,
                "row_version": transition.row_version,
                "attempt_no": attempt.attempt_no,
            },
            summary="Run execution started",
            occurred_at=now,
            run_attempt_id=attempt.id,
        )
        self._session.add_all([event, self._event_outbox(event, status=run.status)])
        return PreparedExecution(
            row_version=transition.row_version,
            next_sequence=sequence + 1,
        )

    async def append_agent_event(
        self,
        claimed: ClaimedRun,
        event: AgentEvent,
        *,
        session_metadata: AgentSessionMetadata,
    ) -> None:
        """Active lease 下で AgentEvent と通知 Outbox を追加する。"""

        if event.event_type is AgentEventType.TEXT_DELTA:
            raise ValueError("Text delta must not be persisted")
        run, _segment, attempt = await self._lock_claimed_execution(claimed)
        now = datetime.now(UTC)
        self._validate_claimed_lease(attempt, claimed, now=now)
        if RunStatus(run.status) is not RunStatus.RUNNING:
            raise LeaseValidationError(f"Run is not running: {run.status}")
        self._validate_agent_event(event, claimed)
        await self._reject_cancelled_execution(run.id)
        next_sequence = await self._next_sequence(run.id)
        if event.sequence < next_sequence:
            raise ConcurrentRunUpdateError("Agent event sequence is not monotonic")
        agent_session = await self._ensure_agent_session(
            claimed,
            sdk_session_id=UUID(event.agent_session_id),
            metadata=session_metadata,
            now=now,
        )
        self._merge_session_usage(agent_session, event)
        stored = self._stored_agent_event(event)
        self._session.add_all([stored, self._event_outbox(stored, status=run.status)])

    async def verify_execution_start(self, claimed: ClaimedRun) -> bool:
        """モデルを起動する直前に、現行 lease と取消を同じ Run lock 下で確認する。"""

        run, segment, attempt = await self._lock_claimed_execution(claimed)
        self._validate_claimed_lease(attempt, claimed, now=datetime.now(UTC))
        if (
            run.project_id != claimed.project_id
            or run.status != RunStatus.RUNNING.value
            or (segment is not None and segment.status != RunSegmentStatus.RUNNING.value)
        ):
            raise LeaseValidationError("Run is not available for model execution")
        return not await self.is_cancellation_requested(run.id)

    async def freeze_agent_task_brief(
        self,
        claimed: ClaimedRun,
        *,
        brief: dict[str, Any],
        checksum: str,
    ) -> None:
        """Active Segment の AgentTaskBrief を checksum 同一性付きで一度だけ固定する。"""

        run, segment, attempt = await self._lock_claimed_execution(claimed)
        if segment is None:
            raise LeaseValidationError("AgentTaskBrief requires an explicit RunSegment")
        now = datetime.now(UTC)
        self._validate_claimed_lease(attempt, claimed, now=now)
        if RunStatus(run.status) is not RunStatus.RUNNING:
            raise LeaseValidationError("AgentTaskBrief can only be frozen while running")
        if await self.is_cancellation_requested(run.id):
            raise RunCancellationRequestedError("AgentTaskBrief preparation was cancelled")
        expected = f"sha256:{sha256_hex(canonical_json(brief))}"
        identity = brief.get("identity")
        if (
            checksum != expected
            or not isinstance(identity, dict)
            or identity.get("run_id") != str(run.id)
            or identity.get("segment_no") != segment.segment_no
        ):
            raise ValueError("AgentTaskBrief identity or checksum does not match the Segment")
        existing = (
            await self._session.scalars(
                select(AgentTaskBriefSnapshot)
                .where(AgentTaskBriefSnapshot.run_segment_id == segment.id)
                .with_for_update()
            )
        ).one_or_none()
        if existing is not None:
            frozen_changed = canonical_json(existing.brief_json) != canonical_json(brief)
            if existing.checksum != checksum or frozen_changed:
                raise ConcurrentRunUpdateError("RunSegment AgentTaskBrief changed after freezing")
            return
        snapshot_id = uuid4()
        snapshot = AgentTaskBriefSnapshot(
            id=snapshot_id,
            run_id=run.id,
            run_segment_id=segment.id,
            brief_version=str(brief.get("brief_version", "")),
            brief_json=brief,
            checksum=checksum,
            created_at=now,
        )
        segment.instruction_snapshot_id = snapshot_id
        segment.updated_at = now
        self._session.add(snapshot)

    async def finalize_execution(
        self,
        claimed: ClaimedRun,
        *,
        target: RunStatus,
        attempt_status: RunAttemptStatus,
        event: AgentEvent | None,
        session_metadata: AgentSessionMetadata | None,
        result: RunResultRecord | None,
        error_json: dict[str, Any] | None,
    ) -> RunStatus:
        """Result/Event/Session/Attempt/Run/Outbox を一つの終態 transaction で確定する。"""

        run, segment, attempt = await self._lock_claimed_execution(claimed)
        now = datetime.now(UTC)
        self._validate_claimed_lease(attempt, claimed, now=now)
        current = RunStatus(run.status)
        if current is not RunStatus.RUNNING:
            if current is target and attempt.status == attempt_status.value:
                return current
            raise LeaseValidationError(f"Run cannot be finalized from {current.value}")
        if target is RunStatus.SUCCEEDED and result is None:
            raise ValueError("Successful Run requires a validated Result")
        if target is not RunStatus.SUCCEEDED and result is not None:
            raise ValueError("Only a successful Run may persist a Result")
        if event is not None:
            self._validate_agent_event(event, claimed)

        # poll と終態化の間に取消が commit されても、同じ Run lock 下の intent が勝つ。
        # 失効 lease は上で拒否済みなので、この上書きは古い Worker の権限を復活させない。
        cancellation_requested = await self.is_cancellation_requested(run.id)
        if cancellation_requested:
            target = RunStatus.CANCELLED
            attempt_status = RunAttemptStatus.CANCELLED
            result = None
            error_json = None
        elif target is RunStatus.CANCELLED:
            raise ValueError("Run cancellation requires a durable cancellation intent")

        transition = plan_run_transition(
            current=current,
            target=target,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.finished_at = transition.finished_at
        run.error_json = error_json
        run.updated_at = now
        attempt.status = attempt_status.value
        attempt.finished_at = now
        attempt.error_json = error_json
        attempt.lease_expires_at = None
        attempt.lease_token_hash = None
        attempt.updated_at = now
        if segment is not None:
            if target is RunStatus.SUCCEEDED:
                segment.status = RunSegmentStatus.COMPLETED.value
            elif target is RunStatus.CANCELLED:
                segment.status = RunSegmentStatus.CANCELLED.value
            elif target in {
                RunStatus.WAITING_PERMISSION,
                RunStatus.WAITING_FOR_INPUT,
                RunStatus.WAITING_FOR_APPROVAL,
            }:
                segment.status = RunSegmentStatus.WAITING.value
            else:
                segment.status = RunSegmentStatus.FAILED.value
            if target not in {
                RunStatus.WAITING_PERMISSION,
                RunStatus.WAITING_FOR_INPUT,
                RunStatus.WAITING_FOR_APPROVAL,
            }:
                segment.finished_at = now
            segment.updated_at = now

        next_sequence = await self._next_sequence(run.id)
        stored_events: list[RunEvent] = []
        sdk_session_id: UUID | None = None
        if event is not None:
            if cancellation_requested:
                # 取消 request 自身が sequence を消費する。platform の終態 event だけは
                # 現在水位で採番し直し、SDK の観測用量と既存 event の一意性を両方保つ。
                event = user_cancellation_event(event, sequence=max(next_sequence, event.sequence))
            if event.sequence < next_sequence:
                raise ConcurrentRunUpdateError("Terminal Agent event sequence is not monotonic")
            sdk_session_id = UUID(event.agent_session_id)
            if session_metadata is None:
                raise ValueError("Agent event requires session metadata")
            agent_session = await self._ensure_agent_session(
                claimed,
                sdk_session_id=sdk_session_id,
                metadata=session_metadata,
                now=now,
            )
            self._merge_session_usage(agent_session, event)
            agent_session.status = _session_status_for(target)
            agent_session.updated_at = now
            terminal_event = self._stored_agent_event(event)
            stored_events.append(terminal_event)
            next_sequence = event.sequence + 1
        else:
            existing_session = await self._find_primary_agent_session(claimed)
            if existing_session is not None:
                sdk_session_id = existing_session.sdk_session_id
                existing_session.status = _session_status_for(target)
                existing_session.updated_at = now

        if result is not None:
            if sdk_session_id is None:
                raise ValueError("Result requires an Agent session")
            self._session.add(
                RunResult(
                    id=uuid4(),
                    run_id=run.id,
                    agent_session_id=sdk_session_id,
                    output_schema=result.output_schema,
                    result_kind=result.result_kind,
                    data_json=result.data,
                    evidence_refs_json=list(result.evidence_refs),
                    artifact_refs_json=list(result.artifact_refs),
                    change_proposal_refs_json=list(result.change_proposal_refs),
                    optional_schema_identity_json=result.optional_schema_identity,
                    summary=result.summary,
                    confidence=result.confidence,
                    needs_review=result.needs_review,
                    usage_json=result.usage,
                    cost_json=result.cost,
                    validation_json=result.validation,
                    created_at=now,
                )
            )

        if target is RunStatus.SUCCEEDED and segment is not None:
            segment_event = RunEvent(
                id=uuid4(),
                run_id=run.id,
                run_attempt_id=attempt.id,
                agent_session_id=sdk_session_id,
                sequence=next_sequence,
                event_type=AgentEventType.SEGMENT_COMPLETED.value,
                payload_json={
                    "run_segment_id": str(segment.id),
                    "segment_no": segment.segment_no,
                },
                occurred_at=now,
                trace_id=None,
                summary="Run segment completed",
            )
            stored_events.append(segment_event)
            next_sequence += 1

        snapshot = self._snapshot_event(
            run.id,
            sequence=next_sequence,
            payload={
                "status": target.value,
                "row_version": transition.row_version,
                "error": error_json,
            },
            summary=f"Run finalized as {target.value}",
            occurred_at=now,
            run_attempt_id=attempt.id,
            agent_session_id=sdk_session_id,
        )
        stored_events.append(snapshot)
        self._session.add_all(
            [
                item
                for stored in stored_events
                for item in (stored, self._event_outbox(stored, status=target.value))
            ]
        )
        return target

    async def heartbeat_attempt(
        self,
        attempt_id: UUID,
        *,
        lease_token_hash: str,
        lease_expires_at: datetime,
        now: datetime,
    ) -> None:
        """Active RunAttempt の token と期限を検証して heartbeat を延長する。"""

        candidate = await self._session.get(RunAttempt, attempt_id)
        if candidate is None:
            raise LeaseValidationError(f"RunAttempt not found: {attempt_id}")
        run = await self._lock_run_row(candidate.run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {candidate.run_id}")
        if candidate.run_segment_id is not None:
            segment = await self._lock_segment_row(
                candidate.run_segment_id, run_id=candidate.run_id
            )
            if segment is None:
                raise LeaseValidationError(f"RunSegment not found: {candidate.run_segment_id}")
        statement = (
            select(RunAttempt)
            .where(RunAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        attempt = (await self._session.scalars(statement)).one_or_none()
        if attempt is None:
            raise LeaseValidationError(f"RunAttempt not found: {attempt_id}")
        # 候補読取から lock 取得までの接管・期限切れを、古い identity map/時刻で通さない。
        extension = lease_expires_at - now
        now = datetime.now(UTC)
        if not hmac.compare_digest(attempt.lease_token_hash or "", lease_token_hash):
            raise LeaseValidationError("RunAttempt lease token does not match")
        if attempt.status not in {
            RunAttemptStatus.LEASED.value,
            RunAttemptStatus.RUNNING.value,
        }:
            raise LeaseValidationError(f"RunAttempt is not active: {attempt.status}")
        if attempt.lease_expires_at is None or attempt.lease_expires_at <= now:
            raise LeaseValidationError("RunAttempt lease has expired")

        attempt.heartbeat_at = now
        attempt.lease_expires_at = now + extension
        attempt.updated_at = now

    async def recover_expired_attempts(self, *, now: datetime, limit: int) -> int:
        """期限切れ Attempt を失効させ、Run を retry queue へ戻す。"""

        attempt_statement = (
            select(RunAttempt)
            .where(
                RunAttempt.status.in_(
                    {RunAttemptStatus.LEASED.value, RunAttemptStatus.RUNNING.value}
                ),
                RunAttempt.lease_expires_at.is_not(None),
                RunAttempt.lease_expires_at <= now,
            )
            .order_by(RunAttempt.lease_expires_at)
            .limit(limit)
        )
        candidates = list((await self._session.scalars(attempt_statement)).all())
        recovered = 0
        for candidate in candidates:
            # 全 recovery path で Run → Segment → Attempt の lock 順を固定し、executor と
            # deadlock しない。歴史 Attempt の null Segment だけは旧順序を維持する。
            run = await self._lock_run_row(candidate.run_id)
            if run is None:
                raise RunNotFoundError(f"Run not found for Attempt: {candidate.run_id}")
            segment = None
            if candidate.run_segment_id is not None:
                segment = await self._lock_segment_row(
                    candidate.run_segment_id, run_id=candidate.run_id
                )
                if segment is None:
                    raise LeaseValidationError(f"RunSegment not found: {candidate.run_segment_id}")
            attempt_statement = (
                select(RunAttempt).where(RunAttempt.id == candidate.id).with_for_update()
            )
            attempt = (await self._session.scalars(attempt_statement)).one_or_none()
            if (
                attempt is None
                or attempt.status
                not in {RunAttemptStatus.LEASED.value, RunAttemptStatus.RUNNING.value}
                or attempt.lease_expires_at is None
                or attempt.lease_expires_at > now
            ):
                # Candidate 選択後に別 Worker が回復済みなら二重 transition しない。
                continue
            current = RunStatus(run.status)
            if current not in {RunStatus.PREPARING, RunStatus.RUNNING}:
                # Run が既に終端へ進んだ場合は Attempt だけを失効させ、Run を再度開かない。
                attempt.status = RunAttemptStatus.LEASE_EXPIRED.value
                attempt.finished_at = now
                attempt.updated_at = now
                await self._close_attempt_sessions(attempt.id, status="FAILED", now=now)
                continue

            cancellation_requested = await self.is_cancellation_requested(run.id)
            if cancellation_requested:
                transition = plan_run_transition(
                    current=current,
                    target=RunStatus.CANCELLED,
                    row_version=run.row_version,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    now=now,
                )
                attempt.status = RunAttemptStatus.CANCELLED.value
                attempt.finished_at = now
                attempt.updated_at = now
                attempt.error_json = None
                run.status = transition.status.value
                run.row_version = transition.row_version
                run.finished_at = transition.finished_at
                run.updated_at = now
                run.error_json = None
                if segment is not None:
                    segment.status = RunSegmentStatus.CANCELLED.value
                    segment.finished_at = now
                    segment.updated_at = now
                await self._close_attempt_sessions(attempt.id, status="INTERRUPTED", now=now)
                event = self._snapshot_event(
                    run.id,
                    sequence=await self._next_sequence(run.id),
                    payload={
                        "status": RunStatus.CANCELLED.value,
                        "row_version": transition.row_version,
                        "error": None,
                    },
                    summary="Run cancellation recovered after lease expiry",
                    occurred_at=now,
                    run_attempt_id=attempt.id,
                )
                self._session.add_all(
                    [event, self._event_outbox(event, status=RunStatus.CANCELLED.value)]
                )
                recovered += 1
                continue

            transition = plan_run_transition(
                current=current,
                target=RunStatus.RETRY_PENDING,
                row_version=run.row_version,
                started_at=run.started_at,
                finished_at=run.finished_at,
                now=now,
            )
            attempt.status = RunAttemptStatus.LEASE_EXPIRED.value
            attempt.finished_at = now
            attempt.updated_at = now
            attempt.error_json = {"code": "lease_expired", "retryable": True}
            run.status = transition.status.value
            run.row_version = transition.row_version
            run.updated_at = now
            run.error_json = {"code": "lease_expired", "retryable": True}
            if segment is not None:
                segment.status = RunSegmentStatus.RUNNING.value
                segment.updated_at = now
            await self._close_attempt_sessions(attempt.id, status="FAILED", now=now)

            event = self._snapshot_event(
                run.id,
                sequence=await self._next_sequence(run.id),
                payload={
                    "status": RunStatus.RETRY_PENDING.value,
                    "row_version": transition.row_version,
                    "error": {"code": "lease_expired", "retryable": True},
                },
                summary="Run lease expired",
                occurred_at=now,
                run_attempt_id=attempt.id,
            )
            dispatch = self._dispatch_outbox(
                run.id,
                payload={"run_id": str(run.id), "reason": "lease_recovery"},
                occurred_at=now,
            )
            self._session.add_all(
                [event, self._event_outbox(event, status=RunStatus.RETRY_PENDING.value), dispatch]
            )
            recovered += 1
        return recovered

    async def _close_attempt_sessions(
        self, attempt_id: UUID, *, status: str, now: datetime
    ) -> None:
        """Lease 回収前の ACTIVE Session を閉じ、次 Attempt の一意制約を解放する。"""

        sessions = (
            await self._session.scalars(
                select(AgentSession)
                .where(
                    AgentSession.run_attempt_id == attempt_id,
                    AgentSession.status == "ACTIVE",
                )
                .with_for_update()
            )
        ).all()
        for session in sessions:
            session.status = status
            session.updated_at = now

    async def _find_by_idempotency(self, command: CreateRunCommand) -> Run:
        """Command の三元 key に一致する既存 Run を取得する。"""

        existing = await self._load_by_idempotency(
            project_id=command.project_id,
            task_id=command.task_id,
            idempotency_key=command.idempotency_key,
        )
        if existing is None:
            # INSERT conflict 後に対象が見えない場合は transaction isolation 異常として扱う。
            raise ConcurrentRunUpdateError("Conflicting Run was not visible in the transaction.")
        return existing

    async def _load_by_idempotency(
        self, *, project_id: UUID, task_id: UUID, idempotency_key: str
    ) -> Run | None:
        """事前照会と INSERT conflict が同じ三元 scope だけを参照する。"""

        statement = select(Run).where(
            Run.project_id == project_id,
            Run.task_id == task_id,
            Run.idempotency_key == idempotency_key,
        )
        return (await self._session.scalars(statement)).one_or_none()

    @staticmethod
    def _to_stored_result(result: RunResult) -> StoredRunResult:
        """ORM Result を raw SDK transcript を含まない read DTO へ変換する。"""

        return StoredRunResult(
            result_id=result.id,
            output_schema=result.output_schema,
            result_kind=result.result_kind,
            data=dict(result.data_json),
            evidence_refs=tuple(result.evidence_refs_json),
            artifact_refs=tuple(result.artifact_refs_json),
            change_proposal_refs=tuple(result.change_proposal_refs_json),
            optional_schema_identity=dict(result.optional_schema_identity_json),
            summary=result.summary,
            confidence=result.confidence,
            needs_review=result.needs_review,
            usage=dict(result.usage_json),
            cost=dict(result.cost_json),
            validation=dict(result.validation_json),
            created_at=result.created_at,
        )


def _session_status_for(target: RunStatus) -> str:
    """Run 終了理由を AgentSession の lifecycle status へ変換する。"""

    if target is RunStatus.SUCCEEDED:
        return "CLOSED"
    if target is RunStatus.CANCELLED:
        return "INTERRUPTED"
    if target in {
        RunStatus.WAITING_PERMISSION,
        RunStatus.WAITING_FOR_INPUT,
        RunStatus.WAITING_FOR_APPROVAL,
    }:
        return "IDLE"
    return "FAILED"


def _implicit_segment(run: Run) -> StoredRunSegment:
    """Release G 前の Run を永続 row を作らず read-only Segment 1 へ投影する。"""

    status = RunStatus(run.status)
    if status is RunStatus.SUCCEEDED:
        segment_status = RunSegmentStatus.COMPLETED
    elif status is RunStatus.FAILED:
        segment_status = RunSegmentStatus.FAILED
    elif status is RunStatus.CANCELLED:
        segment_status = RunSegmentStatus.CANCELLED
    elif status in {
        RunStatus.WAITING_PERMISSION,
        RunStatus.WAITING_FOR_INPUT,
        RunStatus.WAITING_FOR_APPROVAL,
    }:
        segment_status = RunSegmentStatus.WAITING
    elif status is RunStatus.QUEUED:
        segment_status = RunSegmentStatus.CREATED
    else:
        segment_status = RunSegmentStatus.RUNNING
    return StoredRunSegment(
        run_segment_id=None,
        segment_no=1,
        trigger_type=RunSegmentTrigger.INITIAL,
        trigger_ref=None,
        status=segment_status,
        objective={"source": "LEGACY_RUN_SNAPSHOT"},
        checkpoint={},
        continuation_mode=SessionContinuationMode.INITIAL,
        parent_agent_session_id=None,
        task_brief_checksum=None,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
    )


def _to_stored_interaction(
    interaction: UserInteraction,
    response: InteractionResponse | None,
) -> StoredUserInteraction:
    """Interaction/Response ORM row を公開 read model へ変換する。"""

    stored_response = (
        None
        if response is None
        else StoredInteractionResponse(
            response_id=response.id,
            actor_id=response.actor_id,
            interaction_version=response.interaction_version,
            response=dict(response.response_json),
            created_at=response.created_at,
        )
    )
    return StoredUserInteraction(
        interaction_id=interaction.id,
        run_segment_id=interaction.run_segment_id,
        agent_session_id=interaction.agent_session_id,
        interaction_type=UserInteractionType(interaction.interaction_type),
        prompt=dict(interaction.prompt_json),
        options=tuple(dict(item) for item in interaction.options_json),
        required=interaction.required,
        expires_at=interaction.expires_at,
        status=UserInteractionStatus(interaction.status),
        version=interaction.version,
        continuation_mode=SessionContinuationMode(interaction.continuation_mode),
        checkpoint_checksum=interaction.checkpoint_checksum,
        change_proposal_id=interaction.change_proposal_id,
        response=stored_response,
        created_at=interaction.created_at,
    )


class OutboxRepository:
    """未公開 Outbox message を排他的に取得して配送結果を記録する。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped session と lock 済み message cache を保持する。"""

        self._session = session
        self._locked_messages: dict[UUID, OutboxMessage] = {}

    async def lock_pending(
        self, *, topics: frozenset[str], limit: int
    ) -> list[PendingOutboxMessage]:
        """対象 topic の未公開 message を SKIP LOCKED で取得する。"""

        statement = (
            select(OutboxMessage)
            .where(
                OutboxMessage.published_at.is_(None),
                OutboxMessage.topic.in_(topics),
            )
            .order_by(OutboxMessage.occurred_at, OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        messages = list((await self._session.scalars(statement)).all())
        self._locked_messages = {message.id: message for message in messages}
        return [
            PendingOutboxMessage(
                message_id=message.id,
                aggregate_type=message.aggregate_type,
                aggregate_id=message.aggregate_id,
                topic=message.topic,
                payload=message.payload_json,
                publish_attempts=message.publish_attempts,
            )
            for message in messages
        ]

    def mark_published(self, message_id: UUID, *, published_at: datetime) -> None:
        """Lock 済み message を公開済みに更新する。"""

        message = self._locked_messages[message_id]
        message.publish_attempts += 1
        message.published_at = published_at
        message.error_json = None

    def mark_failed(self, message_id: UUID, *, error: Exception) -> None:
        """配送失敗を Secret を含まない短い診断情報として記録する。"""

        message = self._locked_messages[message_id]
        message.publish_attempts += 1
        message.error_json = {
            "type": type(error).__name__,
            "message": "outbox_publish_failed",
        }
