"""Run aggregate の行 lock 順、event 採番、Outbox 生成を共有する repository 基底。"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.agent.domain import AgentEvent, AgentEventType
from projectmind.db.models import (
    AgentSession,
    ChangeProposal,
    EffectExecution,
    Evidence,
    OutboxMessage,
    Run,
    RunAttempt,
    RunEvent,
    RunSegment,
)
from projectmind.runs.domain import (
    AgentSessionMetadata,
    ClaimedRun,
    CreatedRun,
    LeaseValidationError,
    RunAttemptStatus,
    RunNotFoundError,
    RunStatus,
    lease_token_hash,
)


class _RunRepositoryBase:
    """Run 系 repository が共有する transaction 内の共通操作を提供する。

    行 lock は常に Run → Segment → (Attempt / Interaction / Proposal / Effect) の順で
    取得し、RunEvent の sequence 採番と Outbox 生成をここへ集約する。
    """

    def __init__(self, session: AsyncSession) -> None:
        """Repository が利用する transaction-scoped session を保持する。"""

        self._session = session

    async def is_cancellation_requested(self, run_id: UUID) -> bool:
        """Worker が durable な取消 intent を polling できるよう存在だけを返す。"""

        event_id = await self._session.scalar(
            select(RunEvent.id).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "RUN_CANCEL_REQUESTED",
            )
        )
        return event_id is not None

    async def _validate_checkpoint_refs(
        self, run_id: UUID, checkpoint: dict[str, Any]
    ) -> None:
        """Checkpoint の参照が現在 Run の監査資産だけを指すことを保証する。"""

        evidence_refs = checkpoint.get("evidence_refs", [])
        if evidence_refs:
            found = await self._session.scalar(
                select(func.count(Evidence.id)).where(
                    Evidence.run_id == run_id,
                    Evidence.evidence_ref.in_(evidence_refs),
                )
            )
            if int(found or 0) != len(evidence_refs):
                raise ValueError("Interaction checkpoint contains foreign Evidence refs")
        proposal_refs = checkpoint.get("change_proposal_refs", [])
        if proposal_refs:
            found = await self._session.scalar(
                select(func.count(ChangeProposal.id)).where(
                    ChangeProposal.run_id == run_id,
                    ChangeProposal.proposal_ref.in_(proposal_refs),
                )
            )
            if int(found or 0) != len(proposal_refs):
                raise ValueError("Checkpoint contains foreign ChangeProposal refs")
        # Artifact の正本は未導入なので所有権を検証不能な参照は引き続き拒否する。
        if checkpoint.get("artifact_refs"):
            raise ValueError("Interaction checkpoint references an unavailable Artifact")

    async def _lock_run_row(self, run_id: UUID, *, project_id: UUID | None = None) -> Run | None:
        """Aggregate lock 順の先頭として Run 行を FOR UPDATE で取得する。

        Run → Segment → (Attempt / Interaction / Proposal / Effect) の行 lock 順は
        executor・回答 API・recovery が共有する deadlock 回避の不変条件であり、
        他の行 lock より先にこの helper を呼ぶ。project_id を渡した場合は Project 境界の
        越権を不存在と同じ None へ畳む。
        """

        statement = select(Run).where(Run.id == run_id)
        if project_id is not None:
            statement = statement.where(Run.project_id == project_id)
        return (await self._session.scalars(statement.with_for_update())).one_or_none()

    async def _lock_segment_row(self, segment_id: UUID, *, run_id: UUID) -> RunSegment | None:
        """Run lock 取得後の二番目として Segment 行を FOR UPDATE で取得する。"""

        return (
            await self._session.scalars(
                select(RunSegment)
                .where(RunSegment.id == segment_id, RunSegment.run_id == run_id)
                .with_for_update()
            )
        ).one_or_none()

    async def _lock_claimed_execution(
        self, claimed: ClaimedRun
    ) -> tuple[Run, RunSegment | None, RunAttempt]:
        """Run、Segment、Attempt の順に lock し全 Worker transaction を統一する。"""

        run = await self._lock_run_row(claimed.run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {claimed.run_id}")
        segment = None
        if claimed.run_segment_id is not None:
            segment = await self._lock_segment_row(
                claimed.run_segment_id, run_id=claimed.run_id
            )
            if segment is None:
                raise LeaseValidationError(
                    f"RunSegment not found: {claimed.run_segment_id}"
                )
        attempt_statement = (
            select(RunAttempt)
            .where(
                RunAttempt.id == claimed.run_attempt_id,
                RunAttempt.run_id == claimed.run_id,
                *(
                    (RunAttempt.run_segment_id == claimed.run_segment_id,)
                    if claimed.run_segment_id is not None
                    else ()
                ),
            )
            .with_for_update()
        )
        attempt = (await self._session.scalars(attempt_statement)).one_or_none()
        if attempt is None:
            raise LeaseValidationError(f"RunAttempt not found: {claimed.run_attempt_id}")
        return run, segment, attempt

    @staticmethod
    def _validate_claimed_lease(attempt: RunAttempt, claimed: ClaimedRun, *, now: datetime) -> None:
        """Plain lease token を hash 比較し、失効・別 Attempt の更新を拒否する。"""

        token_hash = lease_token_hash(claimed.lease_token)
        if not hmac.compare_digest(attempt.lease_token_hash or "", token_hash):
            raise LeaseValidationError("RunAttempt lease token does not match")
        if attempt.status not in {
            RunAttemptStatus.LEASED.value,
            RunAttemptStatus.RUNNING.value,
        }:
            raise LeaseValidationError(f"RunAttempt is not active: {attempt.status}")
        if attempt.lease_expires_at is None or attempt.lease_expires_at <= now:
            raise LeaseValidationError("RunAttempt lease has expired")

    @staticmethod
    def _validate_agent_event(event: AgentEvent, claimed: ClaimedRun) -> None:
        """別 Run/Attempt の event が aggregate に混入することを拒否する。"""

        if event.run_id != claimed.run_id or event.run_attempt_id != claimed.run_attempt_id:
            raise ValueError("Agent event belongs to a different RunAttempt")

    async def _ensure_agent_session(
        self,
        claimed: ClaimedRun,
        *,
        sdk_session_id: UUID,
        metadata: AgentSessionMetadata,
        now: datetime,
    ) -> AgentSession:
        """Attempt ごとに一つの AgentSession を作成または同一性検証する。"""

        statement = (
            select(AgentSession)
            .where(AgentSession.run_attempt_id == claimed.run_attempt_id)
            .with_for_update()
        )
        existing = (await self._session.scalars(statement)).one_or_none()
        if existing is not None:
            if existing.sdk_session_id != sdk_session_id:
                raise ValueError("RunAttempt Agent session ID changed")
            return existing
        session = AgentSession(
            id=uuid4(),
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            run_segment_id=claimed.run_segment_id,
            sdk_session_id=sdk_session_id,
            parent_session_id=metadata.parent_session_id,
            continuation_mode=metadata.continuation_mode.value,
            checkpoint_checksum=metadata.checkpoint_checksum,
            engine_options_checksum=metadata.engine_options_checksum,
            engine=metadata.engine,
            session_kind=metadata.session_kind.value,
            cwd=metadata.cwd,
            sdk_version=metadata.sdk_version,
            cli_version=metadata.cli_version,
            model=metadata.model,
            status="ACTIVE",
            usage_json={},
            cost_json={},
            created_at=now,
            updated_at=now,
        )
        self._session.add(session)
        return session

    @staticmethod
    def _merge_session_usage(session: AgentSession, event: AgentEvent) -> None:
        """Usage/cost event の公開数値だけを Session metadata へ反映する。"""

        if event.event_type is AgentEventType.USAGE_UPDATED:
            usage = event.payload.get("usage")
            if isinstance(usage, dict):
                session.usage_json = dict(usage)
        total_cost = event.payload.get("total_cost_usd")
        if isinstance(total_cost, int | float):
            session.cost_json = {"total_cost_usd": float(total_cost)}

    async def _next_sequence(self, run_id: UUID) -> int:
        """Lock 済み Run の現在最大 event sequence に一を加える。"""

        statement = select(func.coalesce(func.max(RunEvent.sequence), 0) + 1).where(
            RunEvent.run_id == run_id
        )
        return int((await self._session.scalar(statement)) or 1)

    @staticmethod
    def _stored_agent_event(event: AgentEvent) -> RunEvent:
        """大きな structured output を除外して AgentEvent を監査 row に変換する。"""

        payload = {
            key: value
            for key, value in event.payload.items()
            if key not in {"structured_output", "result"}
        }
        return RunEvent(
            id=uuid4(),
            run_id=event.run_id,
            run_attempt_id=event.run_attempt_id,
            agent_session_id=UUID(event.agent_session_id),
            sequence=event.sequence,
            event_type=event.event_type.value,
            payload_json=payload,
            occurred_at=event.occurred_at,
            trace_id=None,
            summary=event.event_type.value,
        )

    @staticmethod
    def _event_outbox(event: RunEvent, *, status: str) -> OutboxMessage:
        """Event payload 本文を複製せず Redis 通知用 Outbox を生成する。"""

        return OutboxMessage(
            id=uuid4(),
            aggregate_type="run",
            aggregate_id=event.run_id,
            topic="run.lifecycle.changed/v1",
            payload_json={
                "run_id": str(event.run_id),
                "sequence": event.sequence,
                "event_type": event.event_type,
                "status": status,
            },
            occurred_at=event.occurred_at,
            published_at=None,
            publish_attempts=0,
            error_json=None,
        )

    @staticmethod
    def _snapshot_event(
        run_id: UUID,
        *,
        sequence: int,
        payload: dict[str, Any],
        summary: str,
        occurred_at: datetime,
        run_attempt_id: UUID | None = None,
        agent_session_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> RunEvent:
        """Run 状態 snapshot を表す RUN_SNAPSHOT event row を一箇所で生成する。"""

        return RunEvent(
            id=uuid4(),
            run_id=run_id,
            run_attempt_id=run_attempt_id,
            agent_session_id=agent_session_id,
            sequence=sequence,
            event_type="RUN_SNAPSHOT",
            payload_json=payload,
            occurred_at=occurred_at,
            trace_id=trace_id,
            summary=summary,
        )

    @staticmethod
    def _dispatch_outbox(
        run_id: UUID, *, payload: dict[str, Any], occurred_at: datetime
    ) -> OutboxMessage:
        """Run 実行要求を ARQ Queue へ relay する Outbox message を生成する。"""

        return OutboxMessage(
            id=uuid4(),
            aggregate_type="run",
            aggregate_id=run_id,
            topic="run.dispatch.requested/v1",
            payload_json=payload,
            occurred_at=occurred_at,
            published_at=None,
            publish_attempts=0,
            error_json=None,
        )

    @staticmethod
    def _effect_dispatch_outbox(
        execution: EffectExecution, *, occurred_at: datetime
    ) -> OutboxMessage:
        """Approved EffectExecution を専用 Worker queue へ relay する Outbox を生成する。"""

        return OutboxMessage(
            id=uuid4(),
            aggregate_type="effect_execution",
            aggregate_id=execution.id,
            topic="effect.apply.requested/v1",
            payload_json={
                "effect_execution_id": str(execution.id),
                "proposal_id": str(execution.proposal_id),
                "run_id": str(execution.run_id),
            },
            occurred_at=occurred_at,
            published_at=None,
            publish_attempts=0,
            error_json=None,
        )

    @staticmethod
    def _to_created_run(run: Run, *, idempotent_replay: bool) -> CreatedRun:
        """ORM model を application layer が返す不変 DTO へ変換する。"""

        return CreatedRun(
            run_id=run.id,
            project_id=run.project_id,
            task_id=run.task_id,
            status=RunStatus(run.status),
            row_version=run.row_version,
            created_at=run.created_at,
            idempotent_replay=idempotent_replay,
        )
