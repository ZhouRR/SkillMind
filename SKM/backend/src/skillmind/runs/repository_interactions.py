"""UserInteraction の suspend、回答受付、期限切れ recovery を実装する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.db.models import (
    InteractionResponse,
    Run,
    RunEvent,
    RunSegment,
    UserInteraction,
)
from skillmind.runs.domain import (
    AgentSessionMetadata,
    ClaimedRun,
    ConcurrentRunUpdateError,
    InteractionConflictError,
    InteractionExpiredError,
    InteractionNotFoundError,
    LeaseValidationError,
    RespondedInteraction,
    RunAttemptStatus,
    RunNotFoundError,
    RunSegmentStatus,
    RunSegmentTrigger,
    RunStatus,
    SessionContinuationMode,
    UserInteractionStatus,
    UserInteractionType,
    plan_run_transition,
)
from skillmind.runs.interaction import (
    ORDINARY_INTERACTION_TYPES,
    InteractionRequestDraft,
    interaction_response_hash,
    require_ordinary_interaction,
    validate_interaction_option_keys,
    validate_interaction_response,
    validate_interaction_version,
)
from skillmind.runs.repository_base import _RunRepositoryBase


@dataclass(frozen=True, slots=True)
class LockedInteractionResponse:
    """認証再検査の間も保持する Run→Segment→Interaction の内部 lock context。"""

    run: Run = field(repr=False)
    segment: RunSegment = field(repr=False)
    interaction: UserInteraction = field(repr=False)


class InteractionOperationsMixin(_RunRepositoryBase):
    """公開質問の待機・回答・期限切れを同じ lock 順で扱う mixin。"""

    async def suspend_for_interaction(
        self,
        claimed: ClaimedRun,
        *,
        event: AgentEvent,
        session_metadata: AgentSessionMetadata,
        request: InteractionRequestDraft,
    ) -> UUID:
        """公開質問と checkpoint を保存して lease を手放し、Run を待機へ移す。"""

        # Artifact の読取 await 中も原 checkpoint/prompt を差替えさせない。
        request = deepcopy(request)
        run, segment, attempt = await self._lock_claimed_execution(claimed)
        if segment is None:
            raise LeaseValidationError("UserInteraction requires an explicit RunSegment")
        now = datetime.now(UTC)
        self._validate_claimed_lease(attempt, claimed, now=now)
        if RunStatus(run.status) is not RunStatus.RUNNING:
            raise LeaseValidationError(f"Run cannot request interaction from {run.status}")
        self._validate_agent_event(event, claimed)
        if event.event_type is not AgentEventType.INTERACTION_REQUESTED:
            raise ValueError("Interaction suspension requires INTERACTION_REQUESTED")
        require_ordinary_interaction(request.interaction_type)
        validate_interaction_option_keys(request.options)
        await self._reject_cancelled_execution(run.id)
        next_sequence = await self._next_sequence(run.id)
        if event.sequence < next_sequence:
            raise ConcurrentRunUpdateError("Interaction event sequence is not monotonic")
        existing_open = await self._session.scalar(
            select(UserInteraction.id).where(
                UserInteraction.run_id == run.id,
                UserInteraction.status == UserInteractionStatus.OPEN.value,
            )
        )
        if existing_open is not None:
            raise InteractionConflictError("Run already has an open interaction")
        await self._validate_checkpoint_refs(run.id, request.checkpoint)
        self._validate_claimed_lease(attempt, claimed, now=datetime.now(UTC))
        agent_session = await self._ensure_agent_session(
            claimed,
            sdk_session_id=UUID(event.agent_session_id),
            metadata=session_metadata,
            now=now,
        )
        self._merge_session_usage(agent_session, event)
        agent_session.status = "IDLE"
        agent_session.updated_at = now

        interaction_id = uuid4()
        interaction = UserInteraction(
            id=interaction_id,
            run_id=run.id,
            run_segment_id=segment.id,
            agent_session_id=agent_session.id,
            interaction_type=request.interaction_type.value,
            prompt_json=request.prompt,
            options_json=[dict(item) for item in request.options],
            required=request.required,
            expires_at=request.expires_at,
            status=UserInteractionStatus.OPEN.value,
            version=1,
            continuation_mode=request.continuation_mode.value,
            checkpoint_json=request.checkpoint,
            checkpoint_checksum=request.checkpoint_checksum,
            created_at=now,
            updated_at=now,
        )
        target = RunStatus.WAITING_FOR_INPUT
        transition = plan_run_transition(
            current=RunStatus.RUNNING,
            target=target,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.updated_at = now
        run.error_json = None
        segment.status = RunSegmentStatus.WAITING.value
        segment.updated_at = now
        attempt.status = RunAttemptStatus.DEFERRED.value
        attempt.finished_at = now
        attempt.lease_token_hash = None
        attempt.lease_expires_at = None
        attempt.updated_at = now

        interaction_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
            sequence=event.sequence,
            event_type=AgentEventType.INTERACTION_REQUESTED.value,
            payload_json={
                "interaction_id": str(interaction_id),
                "interaction_type": request.interaction_type.value,
                "required": request.required,
                "expires_at": request.expires_at.isoformat(),
                "version": 1,
            },
            occurred_at=event.occurred_at,
            trace_id=None,
            summary="User interaction requested",
        )
        checkpoint_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
            sequence=event.sequence + 1,
            event_type=AgentEventType.CHECKPOINT_CREATED.value,
            payload_json={
                "interaction_id": str(interaction_id),
                "checkpoint_checksum": request.checkpoint_checksum,
                "evidence_refs": list(request.checkpoint.get("evidence_refs", [])),
            },
            occurred_at=now,
            trace_id=None,
            summary="Interaction checkpoint created",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=event.sequence + 2,
            payload={
                "status": target.value,
                "row_version": transition.row_version,
                "run_segment_id": str(segment.id),
                "segment_no": segment.segment_no,
                "interaction_id": str(interaction_id),
                "error": None,
            },
            summary=f"Run waiting for {request.interaction_type.value}",
            occurred_at=now,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
        )
        rows = [interaction, interaction_event, checkpoint_event, snapshot]
        self._session.add_all(
            [
                item
                for row in rows
                for item in (
                    (row, self._event_outbox(row, status=target.value))
                    if isinstance(row, RunEvent)
                    else (row,)
                )
            ]
        )
        return interaction_id

    async def respond_to_interaction(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        interaction_id: UUID,
        actor_id: UUID,
        interaction_version: int,
        response_json: dict[str, Any],
        idempotency_key: str,
        trace_id: str | None,
    ) -> RespondedInteraction:
        """一回限りの回答を追加し、新 Segment と dispatch を同じ transaction で作る。"""

        validate_interaction_version(interaction_version)
        locked = await self.lock_interaction_response(
            project_id=project_id, run_id=run_id, interaction_id=interaction_id,
        )
        return await self.respond_to_locked_interaction(
            locked=locked, actor_id=actor_id, interaction_version=interaction_version,
            response_json=response_json, idempotency_key=idempotency_key, trace_id=trace_id,
        )

    async def lock_interaction_response(
        self, *, project_id: UUID, run_id: UUID, interaction_id: UUID
    ) -> LockedInteractionResponse:
        """状態遷移をせず lock を取得し、service の最終認可前に回答を保存しない。"""

        observed = (
            await self._session.scalars(
                select(UserInteraction).where(
                    UserInteraction.id == interaction_id,
                    UserInteraction.run_id == run_id,
                )
            )
        ).one_or_none()
        if observed is None:
            raise InteractionNotFoundError(f"Interaction not found: {interaction_id}")
        run = await self._lock_run_row(run_id, project_id=project_id, populate_existing=True)
        if run is None:
            raise InteractionNotFoundError(f"Interaction not found: {interaction_id}")
        segment = await self._lock_segment_row(
            observed.run_segment_id, run_id=run_id, populate_existing=True,
        )
        if segment is None:
            raise InteractionNotFoundError(f"Interaction Segment not found: {interaction_id}")
        interaction = await self._lock_interaction_row(
            interaction_id, run_id=run_id, segment_id=segment.id
        )
        return LockedInteractionResponse(run=run, segment=segment, interaction=interaction)

    async def respond_to_locked_interaction(
        self,
        *,
        locked: LockedInteractionResponse,
        actor_id: UUID,
        interaction_version: int,
        response_json: dict[str, Any],
        idempotency_key: str,
        trace_id: str | None,
    ) -> RespondedInteraction:
        """保持済み lock と現在の認可で原回答の再確認または一回限りの回答を行う。"""

        validate_interaction_version(interaction_version)
        run, segment, interaction = locked.run, locked.segment, locked.interaction
        run_id, interaction_id = run.id, interaction.id
        normalized = validate_interaction_response(
            interaction_type=UserInteractionType(interaction.interaction_type),
            prompt=interaction.prompt_json,
            options=tuple(interaction.options_json),
            response=response_json,
        )
        fingerprint = interaction_response_hash(
            interaction_id=interaction_id,
            interaction_version=interaction_version,
            response=normalized,
        )
        existing = (
            await self._session.scalars(
                select(InteractionResponse).where(
                    InteractionResponse.interaction_id == interaction_id
                )
            )
        ).one_or_none()
        if existing is not None:
            # actor は旧 hash に含まれないため、旧値を書き換えず独立した原作者照合を必須にする。
            if (
                existing.actor_id != actor_id
                or existing.run_id != run.id
                or existing.interaction_version != interaction_version
                or existing.idempotency_key != idempotency_key
                or existing.request_hash != fingerprint
            ):
                raise InteractionConflictError("Interaction already has a different response")
            next_segment = (
                await self._session.scalars(
                    select(RunSegment).where(
                        RunSegment.run_id == run_id,
                        RunSegment.trigger_ref == existing.id,
                        RunSegment.trigger_type == RunSegmentTrigger.INTERACTION_RESPONSE.value,
                    )
                )
            ).one()
            return RespondedInteraction(
                run=self._to_created_run(run, idempotent_replay=False),
                interaction_id=interaction_id,
                response_id=existing.id,
                run_segment_id=next_segment.id,
                segment_no=next_segment.segment_no,
                continuation_mode=SessionContinuationMode(next_segment.continuation_mode),
                idempotent_replay=True,
            )
        # 旧監査の原 hash 再確認は保ち、まだ回答のない曖昧な旧質問への新規回答だけを拒否する。
        validate_interaction_option_keys(tuple(interaction.options_json))
        now = datetime.now(UTC)
        if interaction.status != UserInteractionStatus.OPEN.value:
            raise InteractionConflictError("Interaction is no longer open")
        if interaction.version != interaction_version:
            raise InteractionConflictError("Interaction version has changed")
        if interaction.expires_at <= now:
            await self._expire_pending_interaction(
                run=run,
                segment=segment,
                interaction=interaction,
                now=now,
                trace_id=trace_id,
            )
            raise InteractionExpiredError("Interaction has expired")
        expected_status = RunStatus.WAITING_FOR_INPUT
        if (
            RunStatus(run.status) is not expected_status
            or segment.status != RunSegmentStatus.WAITING.value
        ):
            raise InteractionConflictError("Run is not waiting for this interaction")

        response_id = uuid4()
        response = InteractionResponse(
            id=response_id,
            interaction_id=interaction.id,
            run_id=run.id,
            actor_id=actor_id,
            interaction_version=interaction_version,
            idempotency_key=idempotency_key,
            request_hash=fingerprint,
            response_json=normalized,
            created_at=now,
        )
        interaction.status = UserInteractionStatus.RESPONDED.value
        interaction.version += 1
        interaction.updated_at = now
        segment.status = RunSegmentStatus.COMPLETED.value
        segment.finished_at = now
        segment.updated_at = now

        checkpoint = dict(interaction.checkpoint_json)
        prior_responses = checkpoint.get("user_responses")
        user_responses = (
            [dict(item) for item in prior_responses if isinstance(item, dict)]
            if isinstance(prior_responses, list)
            else []
        )
        user_responses.append(
            {
                "interaction_id": str(interaction.id),
                "interaction_type": interaction.interaction_type,
                "response": normalized,
                "responded_at": now.isoformat(),
            }
        )
        checkpoint["user_responses"] = user_responses
        next_segment_no = segment.segment_no + 1
        next_segment_id = uuid4()
        next_segment = RunSegment(
            id=next_segment_id,
            run_id=run.id,
            segment_no=next_segment_no,
            trigger_type=RunSegmentTrigger.INTERACTION_RESPONSE.value,
            trigger_ref=response_id,
            status=RunSegmentStatus.CREATED.value,
            objective_json={
                "text": (
                    "Continue the Run using the audited user response to "
                    f"{interaction.interaction_type}."
                )
            },
            checkpoint_json=checkpoint,
            continuation_mode=interaction.continuation_mode,
            parent_agent_session_id=interaction.agent_session_id,
            instruction_snapshot_id=None,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        transition = plan_run_transition(
            current=expected_status,
            target=RunStatus.QUEUED,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.error_json = None
        run.updated_at = now

        sequence = await self._next_sequence(run.id)
        segment_completed_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence,
            event_type=AgentEventType.SEGMENT_COMPLETED.value,
            payload_json={
                "run_segment_id": str(segment.id),
                "segment_no": segment.segment_no,
                "trigger_type": segment.trigger_type,
                "interaction_id": str(interaction.id),
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="Run segment completed after user response",
        )
        responded_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence + 1,
            event_type=AgentEventType.INTERACTION_RESPONDED.value,
            payload_json={
                "interaction_id": str(interaction.id),
                "interaction_version": interaction_version,
                "response_id": str(response_id),
                "next_run_segment_id": str(next_segment_id),
                "next_segment_no": next_segment_no,
                "continuation_mode": interaction.continuation_mode,
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="User interaction responded",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=sequence + 2,
            payload={
                "status": RunStatus.QUEUED.value,
                "row_version": transition.row_version,
                "run_segment_id": str(next_segment_id),
                "segment_no": next_segment_no,
                "error": None,
            },
            summary="Run queued after user interaction",
            occurred_at=now,
            trace_id=trace_id,
        )
        dispatch = self._dispatch_outbox(
            run.id,
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "reason": "interaction_response",
                "run_segment_id": str(next_segment_id),
            },
            occurred_at=now,
        )
        self._session.add_all(
            [
                response,
                next_segment,
                segment_completed_event,
                self._event_outbox(
                    segment_completed_event, status=RunStatus.QUEUED.value
                ),
                responded_event,
                self._event_outbox(responded_event, status=RunStatus.QUEUED.value),
                snapshot,
                self._event_outbox(snapshot, status=RunStatus.QUEUED.value),
                dispatch,
            ]
        )
        return RespondedInteraction(
            run=self._to_created_run(run, idempotent_replay=False),
            interaction_id=interaction.id,
            response_id=response_id,
            run_segment_id=next_segment_id,
            segment_no=next_segment_no,
            continuation_mode=SessionContinuationMode(interaction.continuation_mode),
            idempotent_replay=False,
        )

    async def _lock_interaction_row(
        self, interaction_id: UUID, *, run_id: UUID, segment_id: UUID
    ) -> UserInteraction:
        """Run→Segment 後に帰属を限定し、lock 前の ORM snapshot を現在行で上書きする。"""

        interaction = (
            await self._session.scalars(
                select(UserInteraction)
                .where(
                    UserInteraction.id == interaction_id,
                    UserInteraction.run_id == run_id,
                    UserInteraction.run_segment_id == segment_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if interaction is None:
            raise InteractionNotFoundError(f"Interaction not found: {interaction_id}")
        return interaction

    async def _expire_pending_interaction(
        self,
        *,
        run: Run,
        segment: RunSegment,
        interaction: UserInteraction,
        now: datetime,
        trace_id: str | None,
    ) -> None:
        """未回答の通常 interaction を閉じ、推測した回答なしで次 Segment へ進める。"""

        if interaction.interaction_type not in ORDINARY_INTERACTION_TYPES:
            raise InteractionConflictError(
                "Effect approval expiry requires the ChangeProposal recovery path"
            )
        if (
            interaction.status != UserInteractionStatus.OPEN.value
            or interaction.expires_at > now
        ):
            raise InteractionConflictError("Interaction is not awaiting expiry")
        interaction.status = UserInteractionStatus.EXPIRED.value
        interaction.version += 1
        interaction.updated_at = now
        # API から期限切れを発見した場合も recovery と同じく、既に進んだ Run は再開しない。
        if (
            RunStatus(run.status) is not RunStatus.WAITING_FOR_INPUT
            or segment.status != RunSegmentStatus.WAITING.value
        ):
            return
        segment.status = RunSegmentStatus.COMPLETED.value
        segment.finished_at = now
        segment.updated_at = now

        # 推薦 option を暗黙の user decision として採用しない。欠落した回答は確認済み事実へ
        # 明記し、Agent が limitation として閉じるか新しい質問を作るかを次 Segment で判断する。
        checkpoint = dict(interaction.checkpoint_json)
        confirmed_facts = [
            str(item)
            for item in checkpoint.get("confirmed_facts", [])
            if isinstance(item, str)
        ]
        confirmed_facts.append(
            "UserInteraction "
            f"{interaction.id} ({interaction.interaction_type}, required="
            f"{str(interaction.required).lower()}) expired without a response at "
            f"{now.isoformat()}; no default response was assumed."
        )
        checkpoint["confirmed_facts"] = confirmed_facts
        next_segment = RunSegment(
            id=uuid4(),
            run_id=run.id,
            segment_no=segment.segment_no + 1,
            trigger_type=RunSegmentTrigger.INTERACTION_TIMEOUT.value,
            trigger_ref=interaction.id,
            status=RunSegmentStatus.CREATED.value,
            objective_json={
                "text": (
                    "Continue safely after the audited user interaction timeout. "
                    "Do not infer a user decision; report required missing information "
                    "as a limitation or request a new interaction."
                )
            },
            checkpoint_json=checkpoint,
            continuation_mode=interaction.continuation_mode,
            parent_agent_session_id=interaction.agent_session_id,
            instruction_snapshot_id=None,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        transition = plan_run_transition(
            current=RunStatus.WAITING_FOR_INPUT,
            target=RunStatus.QUEUED,
            row_version=run.row_version,
            started_at=run.started_at,
            finished_at=run.finished_at,
            now=now,
        )
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.error_json = None
        run.updated_at = now

        sequence = await self._next_sequence(run.id)
        segment_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence,
            event_type=AgentEventType.SEGMENT_COMPLETED.value,
            payload_json={
                "run_segment_id": str(segment.id),
                "segment_no": segment.segment_no,
                "interaction_id": str(interaction.id),
                "reason": "interaction_expired",
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="Run segment completed after user interaction timeout",
        )
        expired_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence + 1,
            event_type=AgentEventType.INTERACTION_EXPIRED.value,
            payload_json={
                "interaction_id": str(interaction.id),
                "interaction_type": interaction.interaction_type,
                "required": interaction.required,
                "next_run_segment_id": str(next_segment.id),
                "next_segment_no": next_segment.segment_no,
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="User interaction expired without an assumed response",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=sequence + 2,
            payload={
                "status": RunStatus.QUEUED.value,
                "row_version": transition.row_version,
                "run_segment_id": str(next_segment.id),
                "segment_no": next_segment.segment_no,
                "interaction_id": str(interaction.id),
                "error": None,
            },
            summary="Run queued after user interaction timeout",
            occurred_at=now,
            trace_id=trace_id,
        )
        dispatch = self._dispatch_outbox(
            run.id,
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "reason": "interaction_expired",
                "run_segment_id": str(next_segment.id),
            },
            occurred_at=now,
        )
        self._session.add_all(
            [
                next_segment,
                segment_event,
                self._event_outbox(segment_event, status=run.status),
                expired_event,
                self._event_outbox(expired_event, status=run.status),
                snapshot,
                self._event_outbox(snapshot, status=run.status),
                dispatch,
            ]
        )

    async def recover_expired_interactions(self, *, now: datetime, limit: int) -> int:
        """未回答の通常 interaction を期限で閉じ、Run continuation を保存する。"""

        if limit <= 0:
            raise ValueError("Interaction recovery limit must be positive")
        candidates = list(
            (
                await self._session.scalars(
                    select(UserInteraction)
                    .where(
                        UserInteraction.status == UserInteractionStatus.OPEN.value,
                        UserInteraction.interaction_type.in_(
                            [item.value for item in ORDINARY_INTERACTION_TYPES]
                        ),
                        UserInteraction.expires_at <= now,
                    )
                    .order_by(UserInteraction.expires_at, UserInteraction.id)
                    .limit(limit)
                )
            ).all()
        )
        recovered = 0
        for candidate in candidates:
            # 回答 API と競合しても一方だけが進むよう、Run → Segment → Interaction の
            # aggregate lock 順を固定する。
            run = await self._lock_run_row(candidate.run_id)
            if run is None:
                raise RunNotFoundError("Interaction recovery Run not found")
            segment = await self._lock_segment_row(candidate.run_segment_id, run_id=run.id)
            if segment is None:
                raise RunNotFoundError("Interaction recovery Segment not found")
            interaction = await self._lock_interaction_row(
                candidate.id, run_id=run.id, segment_id=segment.id
            )
            # 候補走査時刻ではなく、回答との lock 待機後の現在時刻で期限を確定する。
            locked_now = datetime.now(UTC)
            if (
                interaction.status != UserInteractionStatus.OPEN.value
                or interaction.interaction_type not in ORDINARY_INTERACTION_TYPES
                or interaction.expires_at > locked_now
            ):
                continue
            await self._expire_pending_interaction(
                run=run,
                segment=segment,
                interaction=interaction,
                now=locked_now,
                trace_id=None,
            )
            recovered += 1
        return recovered
