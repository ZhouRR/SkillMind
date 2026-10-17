"""Run 履歴・詳細・イベントの読取と公開 DTO 投影を実装する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from skillmind.agent.runtime_policy import uses_modern_runtime
from skillmind.core.timing import timed_async
from skillmind.db.models import (
    AgentSession,
    AgentTaskBriefSnapshot,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    InteractionResponse,
    Run,
    RunAttempt,
    RunEvent,
    RunResult,
    RunSegment,
    RunSkillSnapshot,
    RuntimeManifest,
    ToolCall,
    UserInteraction,
)
from skillmind.effects.projections import (
    stored_change_approval,
    stored_change_proposal,
    stored_effect_execution,
)
from skillmind.runs.domain import (
    AgentSessionKind,
    RunAttemptStatus,
    RunDetail,
    RunHistoryItem,
    RunHistoryPage,
    RunNotFoundError,
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
)
from skillmind.runs.evidence_preview import evidence_preview_metadata
from skillmind.runs.execution_metrics import execution_metrics
from skillmind.runs.repository_base import _RunRepositoryBase
from skillmind.skills.task_catalog import manifest_task_title


class RunQueryOperationsMixin(_RunRepositoryBase):
    """現在 transaction の session で Project 内の実行事実だけを照会する。"""

    @timed_async("run.performance.detail_query")
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
                # 詳細 DTO は checksum のみ使う。凍結原文を含む Brief 正文は取得しない。
                select(AgentTaskBriefSnapshot)
                .options(
                    load_only(
                        AgentTaskBriefSnapshot.run_segment_id,
                        AgentTaskBriefSnapshot.checksum,
                    )
                )
                .where(AgentTaskBriefSnapshot.run_id == run_id)
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
        titles = await self._history_task_titles([run])
        preview_metadata = evidence_preview_metadata(evidence, tool_calls)
        return RunDetail(
            task_title=titles.get(run.id),
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
            result=None if result is None else stored_run_result(result),
            started_at=run.started_at,
            finished_at=run.finished_at,
            execution_metrics=execution_metrics(
                tool_calls,
                modern=uses_modern_runtime(run.task_snapshot_json),
                manual_responses=len(responses),
            ),
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
                    metadata=preview_metadata.get(item.id, dict(item.metadata_json)),
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
            change_proposals=tuple(stored_change_proposal(item) for item in proposals),
            approvals=tuple(stored_change_approval(item) for item in approvals),
            effect_executions=tuple(stored_effect_execution(item) for item in effect_executions),
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
                Run.started_at.label("started_at"),
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
            .where(Run.project_id == project_id, Run.deleted_at.is_(None))
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
                started_at=row.started_at,
            )
            for row in rows
        }

    @timed_async("run.performance.history_query")
    async def list_history(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        statuses: tuple[RunStatus, ...] = (),
        task_id: UUID | None = None,
        trashed: bool = False,
    ) -> RunHistoryPage:
        """Project 内の Run と任意 Result summary を新しい順にページ取得する。

        `task_id` は精確 Task の最新結果、`statuses` はその状態だけに絞る。
        「回答待ち・承認待ちの Run を全部出す」は
        画面側で先頭 page を filter しても作れないため (待機中の Run が古い page にあると
        取りこぼす)、絞り込みは SQL 側で行う。
        """

        statement = (
            select(Run, RunResult)
            # 一覧で使う列だけ取得する。DTO/元の権限・ページ条件は変更しない。
            .options(
                load_only(
                    Run.id,
                    Run.project_id,
                    Run.task_id,
                    Run.status,
                    Run.row_version,
                    Run.created_at,
                    Run.input_json,
                    Run.selected_sources_json,
                    Run.started_at,
                    Run.finished_at,
                ),
                load_only(RunResult.summary, RunResult.confidence, RunResult.needs_review),
            )
            .outerjoin(RunResult, RunResult.run_id == Run.id)
            .where(
                Run.project_id == project_id,
                Run.deleted_at.is_not(None) if trashed else Run.deleted_at.is_(None),
            )
            .order_by(Run.created_at.desc(), Run.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
        if statuses:
            statement = statement.where(Run.status.in_([status.value for status in statuses]))
        if task_id is not None:
            statement = statement.where(Run.task_id == task_id)
        rows = (await self._session.execute(statement)).all()
        has_more = len(rows) > limit
        titles = await self._history_task_titles([run for run, _ in rows[:limit]])
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
                    task_title=titles.get(run.id),
                )
                for run, result in rows[:limit]
            ),
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    async def _history_task_titles(self, runs: list[Run]) -> dict[UUID, str]:
        """ページ分の不変 Skill binding を一括で読み、元 task の名前だけを投影する。"""

        if not runs:
            return {}
        task_ids = {run.id: run.task_id for run in runs}
        rows = (
            await self._session.execute(
                select(RunSkillSnapshot, RuntimeManifest)
                .join(
                    RuntimeManifest,
                    (
                        (RuntimeManifest.skill_version_id == RunSkillSnapshot.skill_version_id)
                        & (RuntimeManifest.checksum == RunSkillSnapshot.manifest_checksum)
                    ),
                )
                .where(RunSkillSnapshot.run_id.in_(task_ids))
            )
        ).all()
        titles: dict[UUID, str] = {}
        for snapshot, manifest in rows:
            # 現在の有効 Skill や最新版へ置換せず、元 Run と一致する task だけを表示する。
            tasks = manifest.manifest_json.get("tasks")
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                key = task.get("key") if isinstance(task, dict) else None
                if (
                    not isinstance(key, str)
                    or derive_task_id(skill_version_id=snapshot.skill_version_id, task_key=key)
                    != task_ids[snapshot.run_id]
                ):
                    continue
                title = manifest_task_title(manifest.manifest_json, key)
                if title:
                    titles[snapshot.run_id] = title
        return titles

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


def stored_run_result(result: RunResult) -> StoredRunResult:
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
