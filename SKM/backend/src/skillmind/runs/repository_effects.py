"""ChangeProposal と controlled effect の承認、実行、recovery を実装する。"""

from __future__ import annotations

import hmac
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.agent.evidence import new_evidence_ref
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    AgentSession,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    Integration,
    PermissionDecision,
    ResourceBinding,
    Run,
    RunEvent,
    RunSegment,
    ToolCall,
    User,
    UserInteraction,
)
from skillmind.documents.library import (
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryBindingRepository,
    document_library_revision,
    parse_document_library_source,
)
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.continuation import validated_effect_result
from skillmind.effects.database_write import (
    DATABASE_WRITE_CAPABILITY,
    build_database_write,
    database_observation_matches,
)
from skillmind.effects.document_command import load_document_effect_command
from skillmind.effects.document_write import LEGACY_DOCUMENT_WRITE_PROVIDER_VERSION
from skillmind.effects.domain import (
    ApprovalDecision,
    ApprovalSource,
    ChangeProposalApprovalForbiddenError,
    ChangeProposalConflictError,
    ChangeProposalDraft,
    ChangeProposalExpiredError,
    ChangeProposalNotFoundError,
    ChangeProposalStatus,
    ChangeProposalValidationError,
    ClaimedEffectExecution,
    DecideProposalCommand,
    EffectExecutionStatus,
    EffectFailure,
    EffectLeaseValidationError,
    EffectProviderResult,
    EffectRiskLevel,
    EffectStepAuthority,
    ProposalDecisionResult,
    StoredChangeApproval,
    StoredChangeProposal,
    StoredEffectExecution,
)
from skillmind.effects.outcomes import (
    UNKNOWN_EFFECT_CODE,
    effect_failure_record,
    effect_requires_reconciliation,
)
from skillmind.effects.policy_repository import EffectPolicyRepository
from skillmind.effects.proposal import proposal_checksum, proposal_content
from skillmind.effects.reconciliation_domain import EffectReconciliationTarget
from skillmind.integrations.domain import (
    IntegrationStatus,
    ResourceBindingLevel,
    binding_checksum,
)
from skillmind.projects.repository import ProjectRepository
from skillmind.runs.domain import (
    AgentSessionMetadata,
    ClaimedRun,
    ConcurrentRunUpdateError,
    LeaseValidationError,
    RunAttemptStatus,
    RunNotFoundError,
    RunSegmentStatus,
    RunSegmentTrigger,
    RunStatus,
    UserInteractionStatus,
    UserInteractionType,
    lease_token_hash,
    plan_run_transition,
)
from skillmind.runs.repository_base import _RunRepositoryBase
from skillmind.skills.capability_blueprint import resolve_capability_blueprint
from skillmind.users.repository import lock_organization


class EffectOperationsMixin(_RunRepositoryBase):
    """observe → propose → apply の承認境界と effect 実行を担う mixin。"""

    async def suspend_for_proposal(
        self,
        claimed: ClaimedRun,
        *,
        event: AgentEvent,
        session_metadata: AgentSessionMetadata,
        draft: ChangeProposalDraft,
    ) -> UUID:
        """Proposal を保存し、既定 approval または exact preauthorization へ分岐する。"""

        # 出力 snapshot を読む間もモデル候補の nested checkpoint を元の内容へ固定する。
        draft = deepcopy(draft)
        self._execution_features.require_effect(draft.capability_version, draft.operation)
        run, segment, attempt = await self._lock_claimed_execution(claimed)
        if segment is None:
            raise LeaseValidationError("ChangeProposal requires an explicit RunSegment")
        now = datetime.now(UTC)
        self._validate_claimed_lease(attempt, claimed, now=now)
        if RunStatus(run.status) is not RunStatus.RUNNING:
            raise LeaseValidationError(f"Run cannot propose a change from {run.status}")
        self._validate_agent_event(event, claimed)
        if event.event_type is not AgentEventType.CHANGE_PROPOSED:
            raise ValueError("Proposal suspension requires CHANGE_PROPOSED")
        await self._reject_cancelled_execution(run.id)
        next_sequence = await self._next_sequence(run.id)
        if event.sequence < next_sequence:
            raise ConcurrentRunUpdateError("ChangeProposal event sequence is not monotonic")
        existing_open = await self._session.scalar(
            select(UserInteraction.id).where(
                UserInteraction.run_id == run.id,
                UserInteraction.status == UserInteractionStatus.OPEN.value,
            )
        )
        if existing_open is not None:
            raise ChangeProposalConflictError("Run already has an open interaction")

        _intent, binding, _integration, provider_payload = await self._validate_proposal_draft(
            claimed,
            run=run,
            draft=draft,
        )
        await self._validate_checkpoint_refs(run.id, draft.checkpoint)
        self._validate_claimed_lease(attempt, claimed, now=datetime.now(UTC))
        await self._validate_evidence_refs(run.id, draft.evidence_refs)
        agent_session = await self._ensure_agent_session(
            claimed,
            sdk_session_id=UUID(event.agent_session_id),
            metadata=session_metadata,
            now=now,
        )
        self._merge_session_usage(agent_session, event)
        agent_session.status = "IDLE"
        agent_session.updated_at = now

        proposal_id = uuid4()
        proposal_ref = f"cp_{uuid4().hex}"
        checkpoint = dict(draft.checkpoint)
        prior_proposals = checkpoint.get("change_proposal_refs", [])
        proposal_refs = (
            [str(item) for item in prior_proposals if isinstance(item, str)]
            if isinstance(prior_proposals, list)
            else []
        )
        proposal_refs.append(proposal_ref)
        checkpoint["change_proposal_refs"] = proposal_refs
        checkpoint_checksum = f"sha256:{sha256_hex(canonical_json(checkpoint))}"
        skill_version_id = UUID(str(run.task_snapshot_json["skill_version_id"]))
        content = proposal_content(
            project_id=run.project_id,
            run_id=run.id,
            run_segment_id=segment.id,
            agent_session_id=agent_session.id,
            skill_version_id=skill_version_id,
            target_binding_id=binding.id,
            integration_id=binding.integration_id,
            draft=draft,
        )
        checksum = proposal_checksum(content)
        capability = resolve_effect_capability(draft.capability_version)
        requested_scope = capability.requested_scope(provider_payload)
        # 事前許可不可の capability (repository.write など) は policy 照合自体を行わない。
        # 「該当 policy が無いから PENDING」ではなく「制度上あり得ない」ことを構造で示す。
        preauthorization = (
            await EffectPolicyRepository(self._session).match(
                project_id=run.project_id,
                integration_id=binding.integration_id,
                capability_version=draft.capability_version,
                operation=draft.operation,
                risk_level=draft.risk_level,
                requested_scope=requested_scope,
                now=now,
            )
            if capability.preauthorizable and binding.integration_id is not None
            else None
        )
        status = (
            ChangeProposalStatus.APPROVED
            if preauthorization is not None
            else ChangeProposalStatus.PENDING_APPROVAL
        )
        proposal = ChangeProposal(
            id=proposal_id,
            proposal_ref=proposal_ref,
            project_id=run.project_id,
            run_id=run.id,
            run_segment_id=segment.id,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.id,
            skill_version_id=skill_version_id,
            target_binding_id=binding.id,
            integration_id=binding.integration_id,
            effect_intent_key=draft.effect_intent_key,
            capability_version=draft.capability_version,
            operation=draft.operation,
            target_json=draft.target,
            summary=draft.summary,
            preview_json={"changes": [dict(item) for item in draft.changes]},
            precondition_json=draft.precondition,
            evidence_refs_json=list(draft.evidence_refs),
            risk_level=draft.risk_level.value,
            reversible=draft.reversible,
            rollback_json=draft.rollback,
            verification_json=draft.verification,
            continuation_mode=draft.continuation_mode,
            checkpoint_json=checkpoint,
            checkpoint_checksum=checkpoint_checksum,
            idempotency_key=draft.idempotency_key,
            request_fingerprint=draft.request_fingerprint,
            status=status.value,
            version=1,
            checksum=checksum,
            expires_at=draft.expires_at,
            created_at=now,
            updated_at=now,
        )

        approval: ChangeApproval | None = None
        execution: EffectExecution | None = None
        interaction: UserInteraction | None = None
        if preauthorization is not None:
            approval = ChangeApproval(
                id=uuid4(),
                proposal_id=proposal_id,
                run_id=run.id,
                source=ApprovalSource.PREAUTHORIZATION.value,
                decision=ApprovalDecision.APPROVED.value,
                actor_id=None,
                preauthorization_id=preauthorization.preauthorization_id,
                proposal_version=1,
                proposal_checksum=checksum,
                idempotency_key=f"preauth:{preauthorization.preauthorization_id}:{proposal_id}",
                request_hash=sha256_hex(
                    canonical_json(
                        {
                            "proposal_id": str(proposal_id),
                            "proposal_checksum": checksum,
                            "preauthorization_id": str(
                                preauthorization.preauthorization_id
                            ),
                            "policy_version": preauthorization.policy_version,
                        }
                    )
                ),
                reason="Matched exact low-risk Project preauthorization",
                created_at=now,
            )
            execution = self._new_effect_execution(
                proposal=proposal,
                approval=approval,
                provider=binding.provider,
                now=now,
            )
        else:
            interaction = UserInteraction(
                id=uuid4(),
                run_id=run.id,
                run_segment_id=segment.id,
                agent_session_id=agent_session.id,
                interaction_type=UserInteractionType.EFFECT_APPROVAL.value,
                prompt_json={
                    "prompt": draft.summary,
                    "rationale": "An external effect requires an independent approval.",
                    "impact": (
                        f"{draft.capability_version} will change the bound "
                        f"{binding.resource_kind} resource."
                    ),
                    "allow_multiple": False,
                },
                options_json=[
                    {
                        "key": "approve",
                        "label": "Approve",
                        "description": "Apply this exact proposal version.",
                        "recommended": False,
                    },
                    {
                        "key": "reject",
                        "label": "Reject",
                        "description": "Continue without applying the proposal.",
                        "recommended": True,
                    },
                ],
                required=True,
                expires_at=draft.expires_at,
                status=UserInteractionStatus.OPEN.value,
                version=1,
                continuation_mode=draft.continuation_mode,
                checkpoint_json=checkpoint,
                checkpoint_checksum=checkpoint_checksum,
                change_proposal_id=proposal_id,
                created_at=now,
                updated_at=now,
            )

        transition = plan_run_transition(
            current=RunStatus.RUNNING,
            target=RunStatus.WAITING_FOR_APPROVAL,
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

        proposed_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
            sequence=event.sequence,
            event_type=AgentEventType.CHANGE_PROPOSED.value,
            payload_json={
                "proposal_id": str(proposal_id),
                "proposal_ref": proposal_ref,
                "proposal_version": 1,
                "proposal_checksum": checksum,
                "risk_level": draft.risk_level.value,
                "status": status.value,
                "expires_at": draft.expires_at.isoformat(),
                "preauthorized": preauthorization is not None,
            },
            occurred_at=event.occurred_at,
            trace_id=None,
            summary="External change proposed",
        )
        decision_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
            sequence=event.sequence + 1,
            event_type=(
                AgentEventType.EFFECT_APPROVED.value
                if preauthorization is not None
                else AgentEventType.INTERACTION_REQUESTED.value
            ),
            payload_json=(
                {
                    "proposal_id": str(proposal_id),
                    "approval_id": str(approval.id) if approval is not None else None,
                    "source": ApprovalSource.PREAUTHORIZATION.value,
                    "preauthorization_id": str(
                        preauthorization.preauthorization_id
                    ),
                }
                if preauthorization is not None
                else {
                    "proposal_id": str(proposal_id),
                    "interaction_id": str(interaction.id) if interaction is not None else None,
                    "interaction_type": UserInteractionType.EFFECT_APPROVAL.value,
                    "required": True,
                    "expires_at": draft.expires_at.isoformat(),
                    "version": 1,
                }
            ),
            occurred_at=now,
            trace_id=None,
            summary=(
                "Effect approved by preauthorization"
                if preauthorization is not None
                else "Effect approval requested"
            ),
        )
        checkpoint_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
            sequence=event.sequence + 2,
            event_type=AgentEventType.CHECKPOINT_CREATED.value,
            payload_json={
                "proposal_id": str(proposal_id),
                "checkpoint_checksum": checkpoint_checksum,
                "evidence_refs": list(checkpoint.get("evidence_refs", [])),
            },
            occurred_at=now,
            trace_id=None,
            summary="ChangeProposal checkpoint created",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=event.sequence + 3,
            payload={
                "status": RunStatus.WAITING_FOR_APPROVAL.value,
                "row_version": transition.row_version,
                "run_segment_id": str(segment.id),
                "segment_no": segment.segment_no,
                "proposal_id": str(proposal_id),
                "interaction_id": str(interaction.id) if interaction is not None else None,
                "effect_execution_id": str(execution.id) if execution is not None else None,
                "error": None,
            },
            summary="Run waiting for controlled effect",
            occurred_at=now,
            run_attempt_id=attempt.id,
            agent_session_id=agent_session.sdk_session_id,
        )
        rows: list[Any] = [proposal]
        if interaction is not None:
            rows.append(interaction)
        if approval is not None and execution is not None:
            rows.extend(
                [
                    approval,
                    execution,
                    self._effect_dispatch_outbox(execution, occurred_at=now),
                ]
            )
        rows.extend(
            [
                proposed_event,
                self._event_outbox(proposed_event, status=run.status),
                decision_event,
                self._event_outbox(decision_event, status=run.status),
                checkpoint_event,
                self._event_outbox(checkpoint_event, status=run.status),
                snapshot,
                self._event_outbox(snapshot, status=run.status),
            ]
        )
        self._session.add_all(rows)
        return proposal_id

    async def decide_change_proposal(
        self, command: DecideProposalCommand
    ) -> ProposalDecisionResult:
        """User approval/rejection を追加し、apply dispatch または次 Segment を原子的に作る。"""

        observed = (
            await self._session.scalars(
                select(ChangeProposal).where(
                    ChangeProposal.id == command.proposal_id,
                    ChangeProposal.run_id == command.run_id,
                )
            )
        ).one_or_none()
        if observed is None:
            raise ChangeProposalNotFoundError("ChangeProposal not found in Run")
        run = await self._lock_run_row(command.run_id, project_id=command.project_id)
        if run is None:
            raise ChangeProposalNotFoundError("ChangeProposal not found in Project")
        initiating_actor_id = run.permission_snapshot_json.get("actor_id")
        if not command.actor_is_administrator and not (
            isinstance(initiating_actor_id, str)
            and hmac.compare_digest(initiating_actor_id, str(command.actor_id))
        ):
            # Project membership だけでは外部 write の批准権を付与しない。指定 approver
            # model 導入前の初版は Run initiator と system ADMIN に限定する。
            raise ChangeProposalApprovalForbiddenError(
                "Only the Run initiator or an administrator may decide this proposal"
            )
        segment = await self._lock_segment_row(observed.run_segment_id, run_id=run.id)
        if segment is None:
            raise ChangeProposalNotFoundError("ChangeProposal Segment not found")
        proposal = (
            await self._session.scalars(
                select(ChangeProposal)
                .where(ChangeProposal.id == command.proposal_id)
                .with_for_update()
            )
        ).one()
        if command.decision is ApprovalDecision.APPROVED:
            self._execution_features.require_effect(proposal.capability_version, proposal.operation)
        fingerprint = sha256_hex(
            canonical_json(
                {
                    "proposal_id": str(command.proposal_id),
                    "proposal_version": command.proposal_version,
                    "proposal_checksum": command.proposal_checksum,
                    "decision": command.decision.value,
                    "reason": command.reason,
                }
            )
        )
        existing = (
            await self._session.scalars(
                select(ChangeApproval).where(ChangeApproval.proposal_id == proposal.id)
            )
        ).one_or_none()
        if existing is not None:
            if (
                existing.idempotency_key != command.idempotency_key
                or existing.request_hash != fingerprint
            ):
                raise ChangeProposalConflictError(
                    "ChangeProposal already has a different approval decision"
                )
            existing_effect = (
                await self._session.scalars(
                    select(EffectExecution).where(
                        EffectExecution.proposal_id == proposal.id
                    )
                )
            ).one_or_none()
            return ProposalDecisionResult(
                proposal=self._stored_change_proposal(proposal),
                approval=self._stored_change_approval(existing),
                effect_execution=(
                    self._stored_effect_execution(existing_effect)
                    if existing_effect is not None
                    else None
                ),
                run_status=run.status,
                idempotent_replay=True,
            )

        now = datetime.now(UTC)
        if proposal.status != ChangeProposalStatus.PENDING_APPROVAL.value:
            raise ChangeProposalConflictError("ChangeProposal is not pending approval")
        if proposal.version != command.proposal_version:
            raise ChangeProposalConflictError("ChangeProposal version is stale")
        if not hmac.compare_digest(proposal.checksum, command.proposal_checksum):
            raise ChangeProposalConflictError("ChangeProposal checksum is stale")
        if RunStatus(run.status) is not RunStatus.WAITING_FOR_APPROVAL:
            raise ChangeProposalConflictError("Run is not waiting for this approval")
        interaction = (
            await self._session.scalars(
                select(UserInteraction)
                .where(
                    UserInteraction.change_proposal_id == proposal.id,
                    UserInteraction.run_id == run.id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if interaction is None or interaction.status != UserInteractionStatus.OPEN.value:
            raise ChangeProposalConflictError("Effect approval interaction is not open")
        if proposal.expires_at <= now or interaction.expires_at <= now:
            await self._expire_pending_proposal(
                run=run,
                segment=segment,
                proposal=proposal,
                interaction=interaction,
                now=now,
                trace_id=command.trace_id,
            )
            raise ChangeProposalExpiredError("ChangeProposal approval has expired")
        await self._validate_proposal_row(proposal, run=run)

        approval = ChangeApproval(
            id=uuid4(),
            proposal_id=proposal.id,
            run_id=run.id,
            source=ApprovalSource.USER.value,
            decision=command.decision.value,
            actor_id=command.actor_id,
            preauthorization_id=None,
            proposal_version=command.proposal_version,
            proposal_checksum=command.proposal_checksum,
            idempotency_key=command.idempotency_key,
            request_hash=fingerprint,
            reason=command.reason,
            created_at=now,
        )
        interaction.status = UserInteractionStatus.RESPONDED.value
        interaction.version += 1
        interaction.updated_at = now

        sequence = await self._next_sequence(run.id)
        effect: EffectExecution | None = None
        if command.decision is ApprovalDecision.APPROVED:
            binding = await self._session.get(ResourceBinding, proposal.target_binding_id)
            if binding is None:
                raise ChangeProposalValidationError("Proposal binding is unavailable")
            proposal.status = ChangeProposalStatus.APPROVED.value
            proposal.updated_at = now
            effect = self._new_effect_execution(
                proposal=proposal,
                approval=approval,
                provider=binding.provider,
                now=now,
            )
            approved_event = RunEvent(
                id=uuid4(),
                run_id=run.id,
                run_attempt_id=None,
                agent_session_id=None,
                sequence=sequence,
                event_type=AgentEventType.EFFECT_APPROVED.value,
                payload_json={
                    "proposal_id": str(proposal.id),
                    "proposal_ref": proposal.proposal_ref,
                    "approval_id": str(approval.id),
                    "source": ApprovalSource.USER.value,
                    "effect_execution_id": str(effect.id),
                },
                occurred_at=now,
                trace_id=command.trace_id,
                summary="External effect approved",
            )
            self._session.add_all(
                [
                    approval,
                    effect,
                    approved_event,
                    self._event_outbox(approved_event, status=run.status),
                    self._effect_dispatch_outbox(effect, occurred_at=now),
                ]
            )
        else:
            proposal.status = ChangeProposalStatus.REJECTED.value
            proposal.updated_at = now
            segment.status = RunSegmentStatus.COMPLETED.value
            segment.finished_at = now
            segment.updated_at = now
            next_segment = self._next_effect_segment(
                run=run,
                segment=segment,
                proposal=proposal,
                trigger_ref=approval.id,
                outcome="REJECTED",
                now=now,
            )
            transition = plan_run_transition(
                current=RunStatus.WAITING_FOR_APPROVAL,
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
                    "proposal_id": str(proposal.id),
                },
                occurred_at=now,
                trace_id=command.trace_id,
                summary="Run segment completed after effect rejection",
            )
            rejected_event = RunEvent(
                id=uuid4(),
                run_id=run.id,
                run_attempt_id=None,
                agent_session_id=None,
                sequence=sequence + 1,
                event_type=AgentEventType.EFFECT_REJECTED.value,
                payload_json={
                    "proposal_id": str(proposal.id),
                    "proposal_ref": proposal.proposal_ref,
                    "approval_id": str(approval.id),
                    "next_run_segment_id": str(next_segment.id),
                    "next_segment_no": next_segment.segment_no,
                },
                occurred_at=now,
                trace_id=command.trace_id,
                summary="External effect rejected",
            )
            snapshot = self._snapshot_event(
                run.id,
                sequence=sequence + 2,
                payload={
                    "status": RunStatus.QUEUED.value,
                    "row_version": transition.row_version,
                    "run_segment_id": str(next_segment.id),
                    "segment_no": next_segment.segment_no,
                    "proposal_id": str(proposal.id),
                    "error": None,
                },
                summary="Run queued after effect rejection",
                occurred_at=now,
                trace_id=command.trace_id,
            )
            dispatch = self._dispatch_outbox(
                run.id,
                payload={
                    "run_id": str(run.id),
                    "project_id": str(run.project_id),
                    "reason": "effect_rejected",
                    "run_segment_id": str(next_segment.id),
                },
                occurred_at=now,
            )
            self._session.add_all(
                [
                    approval,
                    next_segment,
                    segment_event,
                    self._event_outbox(segment_event, status=run.status),
                    rejected_event,
                    self._event_outbox(rejected_event, status=run.status),
                    snapshot,
                    self._event_outbox(snapshot, status=run.status),
                    dispatch,
                ]
            )
        return ProposalDecisionResult(
            proposal=self._stored_change_proposal(proposal),
            approval=self._stored_change_approval(approval),
            effect_execution=(
                self._stored_effect_execution(effect) if effect is not None else None
            ),
            run_status=run.status,
            idempotent_replay=False,
        )

    async def _expire_pending_proposal(
        self,
        *,
        run: Run,
        segment: RunSegment,
        proposal: ChangeProposal,
        interaction: UserInteraction,
        now: datetime,
        trace_id: str | None,
    ) -> None:
        """未回答の effect approval を stale とし、Provider を呼ばず次 Segment へ進める。"""

        proposal.status = ChangeProposalStatus.STALE.value
        proposal.updated_at = now
        interaction.status = UserInteractionStatus.EXPIRED.value
        interaction.version += 1
        interaction.updated_at = now
        segment.status = RunSegmentStatus.COMPLETED.value
        segment.finished_at = now
        segment.updated_at = now
        next_segment = self._next_effect_segment(
            run=run,
            segment=segment,
            proposal=proposal,
            trigger_ref=interaction.id,
            outcome="EXPIRED",
            now=now,
            trigger_type=RunSegmentTrigger.APPROVAL_TIMEOUT,
        )
        transition = plan_run_transition(
            current=RunStatus.WAITING_FOR_APPROVAL,
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
                "proposal_id": str(proposal.id),
                "interaction_id": str(interaction.id),
                "reason": "approval_expired",
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="Run segment completed after effect approval timeout",
        )
        expired_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence + 1,
            event_type=AgentEventType.EFFECT_REJECTED.value,
            payload_json={
                "proposal_id": str(proposal.id),
                "proposal_ref": proposal.proposal_ref,
                "interaction_id": str(interaction.id),
                "status": ChangeProposalStatus.STALE.value,
                "reason": "approval_expired",
                "next_run_segment_id": str(next_segment.id),
                "next_segment_no": next_segment.segment_no,
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="External effect approval expired",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=sequence + 2,
            payload={
                "status": RunStatus.QUEUED.value,
                "row_version": transition.row_version,
                "run_segment_id": str(next_segment.id),
                "segment_no": next_segment.segment_no,
                "proposal_id": str(proposal.id),
                "interaction_id": str(interaction.id),
                "error": None,
            },
            summary="Run queued after effect approval timeout",
            occurred_at=now,
            trace_id=trace_id,
        )
        dispatch = self._dispatch_outbox(
            run.id,
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "reason": "effect_approval_expired",
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

    async def claim_effect_execution(
        self,
        effect_execution_id: UUID,
        *,
        worker_id: str,
        lease_token: str,
        lease_token_hash_value: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedEffectExecution | None:
        """Approved effect を Run → Segment → Effect の lock 順で idempotent に claim する。"""

        if max_attempts <= 0 or type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("Effect lease and max attempts must be positive")
        observed = await self._session.get(EffectExecution, effect_execution_id)
        if observed is None:
            raise LookupError("EffectExecution not found")
        observed_proposal = await self._session.get(ChangeProposal, observed.proposal_id)
        if observed_proposal is None:
            raise LookupError("EffectExecution Proposal not found")
        run = await self._lock_run_row(observed.run_id)
        if run is None:
            raise RunNotFoundError("EffectExecution Run not found")
        segment = await self._lock_segment_row(
            observed_proposal.run_segment_id, run_id=run.id
        )
        if segment is None:
            raise RunNotFoundError("EffectExecution Segment not found")
        proposal = (
            await self._session.scalars(
                select(ChangeProposal)
                .where(ChangeProposal.id == observed_proposal.id)
                .with_for_update()
            )
        ).one()
        execution = (
            await self._session.scalars(
                select(EffectExecution)
                .where(EffectExecution.id == effect_execution_id)
                .with_for_update()
            )
        ).one()
        if execution.status in {
            EffectExecutionStatus.APPLIED.value,
            EffectExecutionStatus.STALE.value,
            EffectExecutionStatus.FAILED.value,
            EffectExecutionStatus.VERIFICATION_FAILED.value,
        }:
            return None
        self._execution_features.require_effect(proposal.capability_version, proposal.operation)
        now = datetime.now(UTC)
        if (
            execution.status in {
                EffectExecutionStatus.LEASED.value,
                EffectExecutionStatus.APPLYING.value,
            }
            and execution.lease_expires_at is not None
            and execution.lease_expires_at > now
        ):
            return None
        if RunStatus(run.status) is not RunStatus.WAITING_FOR_APPROVAL:
            raise EffectLeaseValidationError("Run is not waiting for the approved effect")
        if proposal.status not in {
            ChangeProposalStatus.APPROVED.value,
            ChangeProposalStatus.APPLYING.value,
        }:
            raise EffectLeaseValidationError("Proposal is not approved for apply")
        cancellation_requested = await self.is_cancellation_requested(run.id)
        if cancellation_requested:
            await self._close_effect_before_provider(
                run=run,
                segment=segment,
                proposal=proposal,
                execution=execution,
                status=EffectExecutionStatus.FAILED,
                code="run_cancelled",
                now=now,
            )
            return None
        if proposal.expires_at <= now:
            await self._close_effect_before_provider(
                run=run,
                segment=segment,
                proposal=proposal,
                execution=execution,
                status=EffectExecutionStatus.STALE,
                code="proposal_expired",
                now=now,
            )
            return None
        approval = await self._session.get(ChangeApproval, execution.approval_id)
        if (
            approval is None
            or approval.proposal_id != proposal.id
            or approval.decision != ApprovalDecision.APPROVED.value
            or approval.proposal_version != proposal.version
            or not hmac.compare_digest(approval.proposal_checksum, proposal.checksum)
        ):
            raise EffectLeaseValidationError("Effect approval does not match Proposal version")
        await self._validate_proposal_row(proposal, run=run)
        binding = await self._session.get(ResourceBinding, proposal.target_binding_id)
        integration = (await self._session.get(Integration, proposal.integration_id)
                       if proposal.integration_id is not None else None)
        agent_session = await self._session.get(AgentSession, proposal.agent_session_id)
        if (binding is None or agent_session is None
            or (proposal.integration_id is not None and integration is None)):
            raise EffectLeaseValidationError("Effect execution snapshot is incomplete")
        # 効果は主 Session からしか起こせない (計画 §23 D1) ので、ここへ来る session は必ず
        # PRIMARY = SDK session 有り。扇出の子は SDK 未起動なら NULL を持つため、型上は
        # optional になった。想定が崩れたら黙って進めず閉じる。
        if agent_session.sdk_session_id is None:
            raise EffectLeaseValidationError("Effect execution has no SDK Agent session")
        next_attempt = execution.attempt_no + 1
        if next_attempt > max_attempts:
            await self._close_effect_before_provider(
                run=run,
                segment=segment,
                proposal=proposal,
                execution=execution,
                status=EffectExecutionStatus.FAILED,
                code="retry_exhausted",
                now=now,
            )
            return None

        tool_call = (
            await self._session.get(ToolCall, execution.tool_call_id)
            if execution.tool_call_id is not None
            else None
        )
        if tool_call is None:
            tool_call = ToolCall(
                id=uuid4(),
                run_id=run.id,
                run_attempt_id=proposal.run_attempt_id,
                agent_session_id=agent_session.sdk_session_id,
                sdk_tool_use_id=f"effect:{execution.id}",
                request_fingerprint=execution.request_fingerprint,
                tool_name=("effect__"
                           + proposal.capability_version.replace(".", "_").replace("/", "_")),
                capability_version=proposal.capability_version,
                provider=execution.provider,
                integration_id=binding.integration_id,
                arguments_summary={
                    "target_keys": sorted(proposal.target_json),
                    "change_paths": [
                        str(item.get("path"))
                        for item in proposal.preview_json.get("changes", [])
                        if isinstance(item, dict)
                    ],
                },
                status="RUNNING",
                duration_ms=None,
                result_json=None,
                error_json=None,
                created_at=now,
                updated_at=now,
            )
            self._session.add(tool_call)
            await self._session.flush()
            self._session.add(
                PermissionDecision(
                    id=uuid4(),
                    run_id=run.id,
                    tool_call_id=tool_call.id,
                    policy="controlled_effect",
                    decision="AUTO_ALLOW",
                    decided_by=approval.actor_id,
                    reason=(
                        "Exact user approval"
                        if approval.source == ApprovalSource.USER.value
                        else "Exact low-risk preauthorization"
                    ),
                    request_fingerprint=execution.request_fingerprint,
                    request_json={
                        "proposal_id": str(proposal.id),
                        "proposal_version": proposal.version,
                        "approval_id": str(approval.id),
                    },
                    decided_at=now,
                )
            )
            execution.tool_call_id = tool_call.id
        elif (
            tool_call.request_fingerprint != execution.request_fingerprint
            or tool_call.capability_version != proposal.capability_version
            or tool_call.integration_id != binding.integration_id
        ):
            raise EffectLeaseValidationError("Effect ToolCall snapshot changed")
        else:
            tool_call.status = "RUNNING"
            tool_call.error_json = None
            tool_call.updated_at = now

        # lock/Artifact/ToolCall flush の待機後に lease を開始する。取鎖前の期限を流用しない。
        now = datetime.now(UTC)
        if proposal.expires_at <= now:
            await self._close_effect_before_provider(
                run=run, segment=segment, proposal=proposal, execution=execution,
                status=EffectExecutionStatus.STALE, code="proposal_expired", now=now,
            )
            return None
        lease_expires_at = min(now + timedelta(seconds=lease_seconds), proposal.expires_at)
        execution.status = EffectExecutionStatus.APPLYING.value
        execution.worker_id = worker_id
        execution.lease_token_hash = lease_token_hash_value
        execution.lease_expires_at = lease_expires_at
        execution.heartbeat_at = now
        execution.attempt_no = next_attempt
        execution.started_at = execution.started_at or now
        execution.updated_at = now
        proposal.status = ChangeProposalStatus.APPLYING.value
        proposal.updated_at = now
        changes = proposal.preview_json.get("changes", [])
        return ClaimedEffectExecution(
            effect_execution_id=execution.id,
            proposal_id=proposal.id,
            proposal_ref=proposal.proposal_ref,
            approval_id=approval.id,
            run_id=run.id,
            run_segment_id=segment.id,
            run_attempt_id=proposal.run_attempt_id,
            agent_session_id=agent_session.sdk_session_id,
            project_id=run.project_id,
            integration_id=binding.integration_id,
            binding_id=binding.id,
            capability_version=proposal.capability_version,
            operation=proposal.operation,
            target=dict(proposal.target_json),
            changes=(
                tuple(dict(item) for item in changes if isinstance(item, dict))
                if isinstance(changes, list)
                else ()
            ),
            precondition=dict(proposal.precondition_json),
            verification=dict(proposal.verification_json),
            idempotency_key=proposal.idempotency_key,
            request_fingerprint=proposal.request_fingerprint,
            provider=integration.provider if integration is not None else binding.provider,
            integration_revision=(
                integration.revision if integration is not None else int(binding.revision)
            ),
            integration_scope=dict(binding.scope_json),
            integration_config=dict(integration.config_json) if integration is not None else {},
            secret_reference_id=(
                integration.secret_reference_id if integration is not None else None
            ),
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            attempt_no=next_attempt,
        )

    async def authorize_effect_step(
        self, claimed: ClaimedEffectExecution, *, provider_version: str
    ) -> EffectStepAuthority:
        """遠端段階の直前に、現在 actor・元批准・snapshot・lease を同じ TX で復験する。"""

        self._execution_features.require_effect(claimed.capability_version, claimed.operation)
        # 永続批准の Worker は browser credential を再作成しない。現在の発起人と承認者の
        # 有効性を要求し、Org→User→Project→Run の順序で管理操作と整合させる。
        observed_run = await self._session.get(Run, claimed.run_id)
        observed_approval = await self._session.get(ChangeApproval, claimed.approval_id)
        if observed_run is None or observed_approval is None:
            raise EffectLeaseValidationError("Effect authority is unavailable")
        actor_value = observed_run.permission_snapshot_json.get("actor_id")
        try:
            actor_id = UUID(str(actor_value))
        except (ValueError, TypeError) as error:
            raise EffectLeaseValidationError("Effect initiating actor is unavailable") from error
        approval_actor_id = observed_approval.actor_id
        if approval_actor_id is None or observed_approval.source != ApprovalSource.USER.value:
            raise EffectLeaseValidationError("Effect requires an exact user approval")
        observed_user = await self._session.get(User, actor_id)
        if observed_user is None:
            raise EffectLeaseValidationError("Effect initiating actor is unavailable")
        organization_id = observed_user.organization_id
        await lock_organization(self._session, organization_id)
        users = {}
        for user_id in sorted({actor_id, approval_actor_id}, key=str):
            user = await self._session.scalar(
                select(User)
                .where(User.id == user_id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            if user is None or user.status != "ACTIVE" or user.organization_id != organization_id:
                raise EffectLeaseValidationError("Effect actor authority was revoked")
            users[user_id] = user
        if approval_actor_id != actor_id and users[approval_actor_id].system_role != "ADMIN":
            raise EffectLeaseValidationError("Effect approver authority was revoked")
        projects = ProjectRepository(self._session)
        for user in users.values():
            access = await projects.lock_write_access(user=user, project_id=claimed.project_id)
            projects.require_active_write_access(access)

        run = await self._lock_run_row(claimed.run_id, project_id=claimed.project_id)
        segment = await self._lock_segment_row(claimed.run_segment_id, run_id=claimed.run_id)
        proposal = await self._session.scalar(
            select(ChangeProposal)
            .where(ChangeProposal.id == claimed.proposal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        execution = await self._session.scalar(
            select(EffectExecution)
            .where(EffectExecution.id == claimed.effect_execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        approval = await self._session.get(
            ChangeApproval, claimed.approval_id, populate_existing=True
        )
        now = datetime.now(UTC)
        if (
            run is None
            or segment is None
            or proposal is None
            or execution is None
            or approval is None
            or run.status != RunStatus.WAITING_FOR_APPROVAL.value
            or segment.status != RunSegmentStatus.WAITING.value
            or run.permission_snapshot_json.get("actor_id") != str(actor_id)
            or claimed.capability_version
            not in run.permission_snapshot_json.get("allowed_capabilities", [])
            or proposal.status != ChangeProposalStatus.APPLYING.value
            or proposal.expires_at <= now
            or proposal.run_id != run.id
            or proposal.project_id != run.project_id
            or proposal.run_segment_id != segment.id
            or execution.run_id != run.id
            or execution.proposal_id != proposal.id
            or execution.approval_id != approval.id
            or execution.provider != claimed.provider
            or execution.provider_version != provider_version
            or execution.attempt_no != claimed.attempt_no
            or execution.request_fingerprint != claimed.request_fingerprint
            or execution.idempotency_key != claimed.idempotency_key
            or approval.run_id != run.id
            or approval.proposal_id != proposal.id
            or approval.actor_id != approval_actor_id
            or approval.source != ApprovalSource.USER.value
            or approval.decision != ApprovalDecision.APPROVED.value
            or approval.proposal_version != proposal.version
            or not hmac.compare_digest(approval.proposal_checksum, proposal.checksum)
        ):
            raise EffectLeaseValidationError("Effect authority changed after claim")
        self._validate_effect_lease(execution, claimed, now=now)
        if await self.is_cancellation_requested(run.id):
            raise EffectLeaseValidationError("Effect Run was cancelled")
        binding = await self._session.get(
            ResourceBinding, claimed.binding_id, populate_existing=True
        )
        integration = (await self._session.get(
            Integration, claimed.integration_id, populate_existing=True
        ) if claimed.integration_id is not None else None)
        agent_session = await self._session.get(
            AgentSession, proposal.agent_session_id, populate_existing=True
        )
        if (binding is None or agent_session is None
            or (proposal.integration_id is not None and integration is None)):
            raise EffectLeaseValidationError("Effect snapshot is unavailable")
        await self._validate_proposal_row(proposal, run=run)
        expected = {
            "proposal_ref": proposal.proposal_ref,
            "run_attempt_id": proposal.run_attempt_id,
            "agent_session_id": agent_session.sdk_session_id,
            "binding_id": proposal.target_binding_id,
            "integration_id": proposal.integration_id,
            "capability_version": proposal.capability_version,
            "operation": proposal.operation,
            "target": proposal.target_json,
            "changes": tuple(proposal.preview_json["changes"]),
            "precondition": proposal.precondition_json,
            "verification": proposal.verification_json,
            "idempotency_key": proposal.idempotency_key,
            "request_fingerprint": proposal.request_fingerprint,
            "provider": integration.provider if integration is not None else binding.provider,
            "integration_revision": (
                integration.revision if integration is not None else int(binding.revision)
            ),
            "integration_scope": binding.scope_json,
            "integration_config": integration.config_json if integration is not None else {},
            "secret_reference_id": (
                integration.secret_reference_id if integration is not None else None
            ),
        }
        json_fields = {
            "target", "changes", "precondition", "verification", "integration_scope",
            "integration_config",
        }
        if any(
            canonical_json(getattr(claimed, key)) != canonical_json(value)
            if key in json_fields else getattr(claimed, key) != value
            for key, value in expected.items()
        ):
            raise EffectLeaseValidationError("Claimed effect differs from its approved snapshot")
        # 追加の DB 検証待ちも lease の寿命を消費するため、返却直前の時計で再確認する。
        now = datetime.now(UTC)
        self._validate_effect_lease(execution, claimed, now=now)
        if proposal.expires_at <= now:
            raise EffectLeaseValidationError("Effect proposal expired during authorization")
        return EffectStepAuthority(organization_id=organization_id, actor_id=actor_id)

    async def heartbeat_effect_execution(
        self, claimed: ClaimedEffectExecution, *, provider_version: str, lease_seconds: int,
    ) -> datetime:
        """元批准/実行権が今も有効な場合だけ、取鎖後の時計で lease を延長する。"""

        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("Effect lease duration must be positive")
        capability = resolve_effect_capability(claimed.capability_version)
        if not capability.staged_authorization:
            raise ValueError("Effect Provider does not support staged supervision")
        await self.authorize_effect_step(claimed, provider_version=provider_version)
        execution = await self._session.get(EffectExecution, claimed.effect_execution_id)
        proposal = await self._session.get(ChangeProposal, claimed.proposal_id)
        if execution is None or proposal is None:
            raise EffectLeaseValidationError("Effect heartbeat target is unavailable")
        now = datetime.now(UTC)
        self._validate_effect_lease(execution, claimed, now=now)
        if proposal.expires_at <= now:
            raise EffectLeaseValidationError("Effect approval expired during heartbeat")
        expires_at = min(now + timedelta(seconds=lease_seconds), proposal.expires_at)
        execution.lease_expires_at = expires_at
        execution.heartbeat_at = now
        execution.updated_at = now
        return expires_at

    async def load_effect_reconciliation_target(
        self, *, project_id: UUID, run_id: UUID, effect_execution_id: UUID,
    ) -> EffectReconciliationTarget:
        """現在の参照認可を得た caller が、失効済み write lease に頼らず原要求を再構築する。"""

        run = await self._lock_run_row(run_id, project_id=project_id)
        observed = await self._session.get(EffectExecution, effect_execution_id)
        if run is None or observed is None or observed.run_id != run.id:
            raise LookupError("Original effect was not found")
        proposal = await self._session.scalar(
            select(ChangeProposal).where(ChangeProposal.id == observed.proposal_id)
            .with_for_update(read=True).execution_options(populate_existing=True)
        )
        execution = await self._session.scalar(
            select(EffectExecution).where(EffectExecution.id == effect_execution_id)
            .with_for_update(read=True).execution_options(populate_existing=True)
        )
        if (proposal is None or execution is None or proposal.project_id != project_id
            or proposal.run_id != run.id or execution.run_id != run.id
            or execution.proposal_id != proposal.id or execution.attempt_no < 1
            or not effect_requires_reconciliation(execution.error_json)):
            raise ValueError("Original effect does not require reconciliation")
        capability = resolve_effect_capability(proposal.capability_version)
        approval = await self._session.get(ChangeApproval, execution.approval_id)
        legacy_document = (
            proposal.capability_version == DOCUMENT_WRITE_CAPABILITY
            and execution.provider == "project-library"
            and execution.provider_version == LEGACY_DOCUMENT_WRITE_PROVIDER_VERSION
        )
        if (not capability.staged_authorization
            or (capability.provider_versions.get(execution.provider) != execution.provider_version
                and not legacy_document)
            or execution.idempotency_key != proposal.idempotency_key
            or execution.request_fingerprint != proposal.request_fingerprint
            or approval is None or approval.run_id != run.id or approval.proposal_id != proposal.id
            or approval.source != ApprovalSource.USER.value
            or approval.decision != ApprovalDecision.APPROVED.value
            or approval.proposal_version != proposal.version
            or approval.proposal_checksum != proposal.checksum):
            raise ValueError("Original effect approval does not match")
        # 元批准の期限/発起人の会話失効は新書込を禁じる。現在の照会者による只読とは別判定。
        if proposal.capability_version == DOCUMENT_WRITE_CAPABILITY:
            # 旧版はここだけで原批准/束縛を読む。新批准・claim・PUT の版上限は緩めない。
            payload = await self._validate_proposal_row(
                proposal, run=run, allow_legacy_document_read=True
            )
        else:
            payload = await self._validate_proposal_row(proposal, run=run)
        binding = await self._session.get(ResourceBinding, proposal.target_binding_id)
        if binding is None:
            raise ValueError("Original effect binding is unavailable")
        if proposal.capability_version == DATABASE_WRITE_CAPABILITY:
            if proposal.integration_id is None:
                raise ValueError("Original database effect has no Integration")
            integration = await self._session.get(Integration, proposal.integration_id)
            if integration is None:
                raise ValueError("Original database Integration is unavailable")
            command = build_database_write(
                effect_id=execution.id, project_id=project_id, run_id=run_id,
                integration_id=integration.id, scope=binding.scope_json, **payload,
            )
            return EffectReconciliationTarget(
                proposal.id, binding.id, proposal.checksum,
                execution.provider, execution.provider_version,
                command, canonical_json(integration.config_json), integration.secret_reference_id,
            )
        if proposal.capability_version != DOCUMENT_WRITE_CAPABILITY:
            raise ValueError("Original effect Provider does not support reconciliation")
        if self._document_library_target is None:
            raise ValueError("Original document storage is unavailable")
        revision = document_library_revision(binding.scope_json)
        if (revision == "1") != legacy_document:
            raise ValueError("Original document protocol does not match its binding")
        object_command = await load_document_effect_command(
            self._session, effect_id=execution.id, project_id=project_id, run_id=run_id,
            payload=payload, target=self._document_library_target,
            protocol_version=int(revision),
        )
        return EffectReconciliationTarget(
            proposal.id, binding.id, proposal.checksum,
            execution.provider, execution.provider_version,
            object_command, "{}", None,
        )

    async def finalize_effect_execution(
        self,
        claimed: ClaimedEffectExecution,
        *,
        result: EffectProviderResult | None,
        failure: EffectFailure | None,
        duration_ms: int,
        trace_id: str | None = None,
    ) -> StoredEffectExecution:
        """Effect result/Evidence と次 Segment を一 transaction で確定する。"""

        if (result is None) == (failure is None):
            raise ValueError("Effect finalization requires exactly one result or failure")
        observed = await self._session.get(EffectExecution, claimed.effect_execution_id)
        if observed is None:
            raise LookupError("EffectExecution not found")
        proposal_observed = await self._session.get(ChangeProposal, observed.proposal_id)
        if proposal_observed is None:
            raise LookupError("EffectExecution Proposal not found")
        run = await self._lock_run_row(claimed.run_id)
        if run is None:
            raise RunNotFoundError("EffectExecution Run not found")
        segment = await self._lock_segment_row(claimed.run_segment_id, run_id=run.id)
        if segment is None:
            raise RunNotFoundError("EffectExecution Segment not found")
        proposal = (
            await self._session.scalars(
                select(ChangeProposal)
                .where(ChangeProposal.id == proposal_observed.id)
                .with_for_update()
            )
        ).one()
        execution = (
            await self._session.scalars(
                select(EffectExecution)
                .where(EffectExecution.id == claimed.effect_execution_id)
                .with_for_update()
            )
        ).one()
        if execution.status in {
            EffectExecutionStatus.APPLIED.value,
            EffectExecutionStatus.STALE.value,
            EffectExecutionStatus.FAILED.value,
            EffectExecutionStatus.VERIFICATION_FAILED.value,
        }:
            return self._stored_effect_execution(execution)
        now = datetime.now(UTC)
        self._validate_effect_lease(execution, claimed, now=now)
        if RunStatus(run.status) is not RunStatus.WAITING_FOR_APPROVAL:
            raise EffectLeaseValidationError("Run left effect waiting state")
        tool_call = (
            await self._session.get(ToolCall, execution.tool_call_id, with_for_update=True)
            if execution.tool_call_id is not None
            else None
        )
        if tool_call is None:
            raise EffectLeaseValidationError("Effect ToolCall is unavailable")

        evidence_refs: tuple[str, ...] = ()
        effect_result: dict[str, Any] | None = None
        if result is not None:
            before_ref = new_evidence_ref()
            after_ref = new_evidence_ref()
            before = self._effect_evidence_row(
                run_id=run.id,
                tool_call_id=tool_call.id,
                evidence_ref=before_ref,
                draft=result.before,
                now=now,
            )
            after = self._effect_evidence_row(
                run_id=run.id,
                tool_call_id=tool_call.id,
                evidence_ref=after_ref,
                draft=result.after,
                now=now,
            )
            effect_result = validated_effect_result({
                "effect_execution_id": str(execution.id),
                "proposal_ref": proposal.proposal_ref,
                "capability_version": proposal.capability_version,
                "status": "APPLIED",
                "after_ref": after_ref,
                "after_content_hash": after.content_hash,
                "after": after.metadata_json["snapshot"],
                "verification": dict(result.verification),
            })
            self._session.add_all([before, after])
            execution.status = EffectExecutionStatus.APPLIED.value
            execution.before_ref = before_ref
            execution.after_ref = after_ref
            execution.verification_json = dict(result.verification)
            execution.error_json = None
            proposal.status = ChangeProposalStatus.APPLIED.value
            tool_call.status = "SUCCEEDED"
            tool_call.result_json = {
                "status": "success",
                "provider": execution.provider,
                "proposal_ref": proposal.proposal_ref,
                "before_ref": before_ref,
                "after_ref": after_ref,
                "verification": dict(result.verification),
                "replayed": result.replayed,
            }
            tool_call.error_json = None
            outcome = "APPLIED"
            evidence_refs = (before_ref, after_ref)
            event_type = AgentEventType.EFFECT_APPLIED
        else:
            assert failure is not None
            execution.status = failure.status.value
            execution.error_json = effect_failure_record(
                capability=proposal.capability_version, attempt_no=execution.attempt_no,
                code=failure.code, retryable=failure.retryable, previous=execution.error_json,
            )
            proposal.status = (
                ChangeProposalStatus.STALE.value
                if failure.status is EffectExecutionStatus.STALE
                else ChangeProposalStatus.FAILED.value
            )
            tool_call.status = "FAILED"
            tool_call.result_json = None
            tool_call.error_json = _effect_tool_error(
                code=execution.error_json["code"],
                retryable=failure.retryable,
            )
            outcome = failure.status.value
            event_type = AgentEventType.EFFECT_FAILED
        execution.executed_at = now
        execution.lease_token_hash = None
        execution.lease_expires_at = None
        execution.heartbeat_at = now
        execution.updated_at = now
        proposal.updated_at = now
        tool_call.duration_ms = duration_ms
        tool_call.updated_at = now
        cancellation_requested = await self._session.scalar(
            select(RunEvent.id).where(
                RunEvent.run_id == run.id,
                RunEvent.event_type == "RUN_CANCEL_REQUESTED",
            )
        )
        if (failure is not None and effect_requires_reconciliation(execution.error_json)
            and (cancellation_requested is not None or not failure.retryable)):
            await self._stop_run_for_unknown_effect(
                run=run, segment=segment, proposal=proposal, execution=execution,
                cancelled=cancellation_requested is not None, now=now, trace_id=trace_id,
            )
            return self._stored_effect_execution(execution)
        if cancellation_requested is not None:
            # Provider 呼び出し中の cancel は外部結果の監査を捨てない。Effect event を先に保存し、
            # terminal RUN_SNAPSHOT を必ず最後に置いて Run を再 dispatch せず閉じる。
            segment.status = RunSegmentStatus.CANCELLED.value
            segment.finished_at = now
            segment.updated_at = now
            transition = plan_run_transition(
                current=RunStatus.WAITING_FOR_APPROVAL,
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
            sequence = await self._next_sequence(run.id)
            effect_event = RunEvent(
                id=uuid4(),
                run_id=run.id,
                run_attempt_id=None,
                agent_session_id=None,
                sequence=sequence,
                event_type=event_type.value,
                payload_json={
                    "proposal_id": str(proposal.id),
                    "proposal_ref": proposal.proposal_ref,
                    "effect_execution_id": str(execution.id),
                    "status": execution.status,
                    "before_ref": execution.before_ref,
                    "after_ref": execution.after_ref,
                    "cancel_requested": True,
                    "error": execution.error_json,
                },
                occurred_at=now,
                trace_id=trace_id,
                summary=f"Controlled effect {outcome} before cancellation",
            )
            snapshot = self._snapshot_event(
                run.id,
                sequence=sequence + 1,
                payload={
                    "status": RunStatus.CANCELLED.value,
                    "row_version": transition.row_version,
                    "run_segment_id": str(segment.id),
                    "segment_no": segment.segment_no,
                    "proposal_id": str(proposal.id),
                    "effect_execution_id": str(execution.id),
                    "error": None,
                },
                summary="Run cancelled after active controlled effect",
                occurred_at=now,
                trace_id=trace_id,
            )
            self._session.add_all(
                [
                    effect_event,
                    self._event_outbox(effect_event, status=run.status),
                    snapshot,
                    self._event_outbox(snapshot, status=run.status),
                ]
            )
            return self._stored_effect_execution(execution)
        if failure is not None and failure.retryable:
            # Transport の一時障害は同一 Proposal/Effect/idempotency key のまま再 dispatch する。
            # User decision 済み Segment を閉じたり新 Segment を作ると、技術 retry が業務上の
            # continuation に見えるため WAITING 状態を維持する。
            execution.status = EffectExecutionStatus.REQUESTED.value
            execution.worker_id = None
            proposal.status = ChangeProposalStatus.APPROVED.value
            sequence = await self._next_sequence(run.id)
            retry_event = RunEvent(
                id=uuid4(),
                run_id=run.id,
                run_attempt_id=None,
                agent_session_id=None,
                sequence=sequence,
                event_type=AgentEventType.EFFECT_FAILED.value,
                payload_json={
                    "proposal_id": str(proposal.id),
                    "proposal_ref": proposal.proposal_ref,
                    "effect_execution_id": str(execution.id),
                    "status": execution.status,
                    "attempt_no": execution.attempt_no,
                    "retry_scheduled": True,
                    "error": execution.error_json,
                },
                occurred_at=now,
                trace_id=trace_id,
                summary="Controlled effect retry scheduled",
            )
            self._session.add_all(
                [
                    retry_event,
                    self._event_outbox(retry_event, status=run.status),
                    self._effect_dispatch_outbox(execution, occurred_at=now),
                ]
            )
            return self._stored_effect_execution(execution)
        segment.status = RunSegmentStatus.COMPLETED.value
        segment.finished_at = now
        segment.updated_at = now
        next_segment = self._next_effect_segment(
            run=run,
            segment=segment,
            proposal=proposal,
            trigger_ref=execution.id,
            outcome=outcome,
            evidence_refs=evidence_refs,
            effect_result=effect_result,
            now=now,
        )
        transition = plan_run_transition(
            current=RunStatus.WAITING_FOR_APPROVAL,
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
                "proposal_id": str(proposal.id),
                "effect_execution_id": str(execution.id),
            },
            occurred_at=now,
            trace_id=trace_id,
            summary="Run segment completed after controlled effect",
        )
        effect_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence + 1,
            event_type=event_type.value,
            payload_json={
                "proposal_id": str(proposal.id),
                "proposal_ref": proposal.proposal_ref,
                "effect_execution_id": str(execution.id),
                "status": execution.status,
                "before_ref": execution.before_ref,
                "after_ref": execution.after_ref,
                "next_run_segment_id": str(next_segment.id),
                "next_segment_no": next_segment.segment_no,
                "error": execution.error_json,
            },
            occurred_at=now,
            trace_id=trace_id,
            summary=f"Controlled effect {outcome}",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=sequence + 2,
            payload={
                "status": RunStatus.QUEUED.value,
                "row_version": transition.row_version,
                "run_segment_id": str(next_segment.id),
                "segment_no": next_segment.segment_no,
                "proposal_id": str(proposal.id),
                "effect_execution_id": str(execution.id),
                "error": None,
            },
            summary="Run queued after controlled effect",
            occurred_at=now,
            trace_id=trace_id,
        )
        dispatch = self._dispatch_outbox(
            run.id,
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "reason": "effect_completed",
                "run_segment_id": str(next_segment.id),
            },
            occurred_at=now,
        )
        self._session.add_all(
            [
                next_segment,
                segment_event,
                self._event_outbox(segment_event, status=run.status),
                effect_event,
                self._event_outbox(effect_event, status=run.status),
                snapshot,
                self._event_outbox(snapshot, status=run.status),
                dispatch,
            ]
        )
        return self._stored_effect_execution(execution)

    async def _close_effect_before_provider(
        self,
        *,
        run: Run,
        segment: RunSegment,
        proposal: ChangeProposal,
        execution: EffectExecution,
        status: EffectExecutionStatus,
        code: str,
        now: datetime,
    ) -> None:
        """Provider を呼べない終局理由を監査し、Run を取消または次 Segment へ進める。"""

        if status not in {EffectExecutionStatus.STALE, EffectExecutionStatus.FAILED}:
            raise ValueError("Pre-provider effect outcome must be STALE or FAILED")
        error = effect_failure_record(
            capability=proposal.capability_version, attempt_no=execution.attempt_no,
            code=code, retryable=False, previous=execution.error_json,
        )
        execution.status = status.value
        execution.error_json = error
        execution.worker_id = None
        execution.lease_token_hash = None
        execution.lease_expires_at = None
        execution.heartbeat_at = now
        execution.executed_at = now
        execution.updated_at = now
        proposal.status = (
            ChangeProposalStatus.STALE.value
            if status is EffectExecutionStatus.STALE
            else ChangeProposalStatus.FAILED.value
        )
        proposal.updated_at = now
        tool_call = (
            await self._session.get(ToolCall, execution.tool_call_id, with_for_update=True)
            if execution.tool_call_id is not None
            else None
        )
        if tool_call is not None:
            tool_call.status = "FAILED"
            tool_call.result_json = None
            tool_call.error_json = _effect_tool_error(code=error["code"], retryable=False)
            tool_call.updated_at = now

        cancellation_requested = await self.is_cancellation_requested(run.id)
        if effect_requires_reconciliation(error):
            await self._stop_run_for_unknown_effect(
                run=run, segment=segment, proposal=proposal, execution=execution,
                cancelled=cancellation_requested, now=now,
            )
            return
        sequence = await self._next_sequence(run.id)
        effect_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence,
            event_type=AgentEventType.EFFECT_FAILED.value,
            payload_json={
                "proposal_id": str(proposal.id),
                "proposal_ref": proposal.proposal_ref,
                "effect_execution_id": str(execution.id),
                "status": status.value,
                "before_ref": None,
                "after_ref": None,
                "error": error,
            },
            occurred_at=now,
            trace_id=None,
            summary=f"Controlled effect closed before Provider: {code}",
        )
        if cancellation_requested:
            # apply 開始前の cancel は Provider を呼ばず、effect event の後に
            # terminal snapshot を置く。
            segment.status = RunSegmentStatus.CANCELLED.value
            segment.finished_at = now
            segment.updated_at = now
            transition = plan_run_transition(
                current=RunStatus.WAITING_FOR_APPROVAL,
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
            snapshot = self._snapshot_event(
                run.id,
                sequence=sequence + 1,
                payload={
                    "status": RunStatus.CANCELLED.value,
                    "row_version": transition.row_version,
                    "run_segment_id": str(segment.id),
                    "segment_no": segment.segment_no,
                    "proposal_id": str(proposal.id),
                    "effect_execution_id": str(execution.id),
                    "error": None,
                },
                summary="Run cancelled before controlled effect Provider",
                occurred_at=now,
            )
            self._session.add_all(
                [
                    effect_event,
                    self._event_outbox(effect_event, status=run.status),
                    snapshot,
                    self._event_outbox(snapshot, status=run.status),
                ]
            )
            return

        segment.status = RunSegmentStatus.COMPLETED.value
        segment.finished_at = now
        segment.updated_at = now
        next_segment = self._next_effect_segment(
            run=run,
            segment=segment,
            proposal=proposal,
            trigger_ref=execution.id,
            outcome=status.value,
            now=now,
        )
        transition = plan_run_transition(
            current=RunStatus.WAITING_FOR_APPROVAL,
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
        segment_event = RunEvent(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=None,
            agent_session_id=None,
            sequence=sequence + 1,
            event_type=AgentEventType.SEGMENT_COMPLETED.value,
            payload_json={
                "run_segment_id": str(segment.id),
                "segment_no": segment.segment_no,
                "proposal_id": str(proposal.id),
                "effect_execution_id": str(execution.id),
            },
            occurred_at=now,
            trace_id=None,
            summary="Run segment completed after pre-provider effect failure",
        )
        snapshot = self._snapshot_event(
            run.id,
            sequence=sequence + 2,
            payload={
                "status": RunStatus.QUEUED.value,
                "row_version": transition.row_version,
                "run_segment_id": str(next_segment.id),
                "segment_no": next_segment.segment_no,
                "proposal_id": str(proposal.id),
                "effect_execution_id": str(execution.id),
                "error": None,
            },
            summary="Run queued after pre-provider effect failure",
            occurred_at=now,
        )
        dispatch = self._dispatch_outbox(
            run.id,
            payload={
                "run_id": str(run.id),
                "project_id": str(run.project_id),
                "reason": "effect_failed_before_provider",
                "run_segment_id": str(next_segment.id),
            },
            occurred_at=now,
        )
        self._session.add_all(
            [
                next_segment,
                effect_event,
                self._event_outbox(effect_event, status=run.status),
                segment_event,
                self._event_outbox(segment_event, status=run.status),
                snapshot,
                self._event_outbox(snapshot, status=run.status),
                dispatch,
            ]
        )

    async def _stop_run_for_unknown_effect(
        self, *, run: Run, segment: RunSegment, proposal: ChangeProposal,
        execution: EffectExecution, cancelled: bool, now: datetime,
        trace_id: str | None = None,
    ) -> None:
        """未確定の遠端事実を残して主処理を止め、新 Segment/新書込を自動生成しない。"""

        transition = plan_run_transition(
            current=RunStatus.WAITING_FOR_APPROVAL,
            target=RunStatus.CANCELLED if cancelled else RunStatus.FAILED,
            row_version=run.row_version, started_at=run.started_at,
            finished_at=run.finished_at, now=now,
        )
        segment.status = (
            RunSegmentStatus.CANCELLED if cancelled else RunSegmentStatus.FAILED
        ).value
        segment.finished_at = now
        segment.updated_at = now
        run.status = transition.status.value
        run.row_version = transition.row_version
        run.finished_at = transition.finished_at
        run.error_json = {
            "code": UNKNOWN_EFFECT_CODE,
            "message": "Original external write result requires reconciliation",
            "effect_execution_id": str(execution.id),
            "retryable": False,
        }
        run.updated_at = now
        sequence = await self._next_sequence(run.id)
        effect_event = RunEvent(
            id=uuid4(), run_id=run.id, run_attempt_id=None, agent_session_id=None,
            sequence=sequence, event_type=AgentEventType.EFFECT_FAILED.value,
            payload_json={
                "proposal_id": str(proposal.id), "proposal_ref": proposal.proposal_ref,
                "effect_execution_id": str(execution.id), "status": execution.status,
                "before_ref": execution.before_ref, "after_ref": execution.after_ref,
                "cancel_requested": cancelled, "error": execution.error_json,
            },
            occurred_at=now, trace_id=trace_id,
            summary="Controlled effect stopped with original write result unresolved",
        )
        snapshot = self._snapshot_event(
            run.id, sequence=sequence + 1,
            payload={
                "status": run.status, "row_version": run.row_version,
                "run_segment_id": str(segment.id), "segment_no": segment.segment_no,
                "proposal_id": str(proposal.id), "effect_execution_id": str(execution.id),
                "error": run.error_json,
            },
            summary="Run stopped pending original external write reconciliation",
            occurred_at=now, trace_id=trace_id,
        )
        self._session.add_all([
            effect_event, self._event_outbox(effect_event, status=run.status),
            snapshot, self._event_outbox(snapshot, status=run.status),
        ])

    @staticmethod
    def _validate_effect_lease(
        execution: EffectExecution,
        claimed: ClaimedEffectExecution,
        *,
        now: datetime,
    ) -> None:
        """Raw lease を保存せず hash と identity/expiry を constant-time 検証する。"""

        if (
            execution.id != claimed.effect_execution_id
            or execution.status != EffectExecutionStatus.APPLYING.value
            or execution.lease_token_hash is None
            or execution.lease_expires_at is None
            or execution.lease_expires_at <= now
            or not hmac.compare_digest(
                execution.lease_token_hash,
                lease_token_hash(claimed.lease_token),
            )
        ):
            raise EffectLeaseValidationError("EffectExecution lease is invalid or expired")

    @staticmethod
    def _effect_evidence_row(
        *,
        run_id: UUID,
        tool_call_id: UUID,
        evidence_ref: str,
        draft: Any,
        now: datetime,
    ) -> Evidence:
        """Provider draft を content hash と scoped snapshot metadata 付き Evidence へ変換する。"""

        content = dict(draft.content)
        return Evidence(
            id=uuid4(),
            evidence_ref=evidence_ref,
            run_id=run_id,
            tool_call_id=tool_call_id,
            evidence_type=draft.evidence_type,
            source_uri=draft.source_uri,
            source_locator=dict(draft.source_locator),
            content_hash=f"sha256:{sha256_hex(canonical_json(content))}",
            snapshot_uri=None,
            excerpt=draft.excerpt,
            metadata_json={**dict(draft.metadata), "snapshot": content},
            created_at=now,
        )

    async def _validate_evidence_refs(
        self, run_id: UUID, evidence_refs: tuple[str, ...]
    ) -> None:
        """Proposal の根拠が同一 Run の Evidence だけを指すことを保証する。"""

        found = await self._session.scalar(
            select(func.count(Evidence.id)).where(
                Evidence.run_id == run_id,
                Evidence.evidence_ref.in_(evidence_refs),
            )
        )
        if int(found or 0) != len(evidence_refs):
            raise ChangeProposalValidationError("ChangeProposal references foreign Evidence")

    async def _validate_database_observation(
        self, draft: ChangeProposalDraft, *, binding: ResourceBinding, payload: dict[str, Any]
    ) -> None:
        """DB 提案は同じ Run/binding の成功した read Tool 証拠に束縛し、自己申告を拒否する。"""

        if draft.capability_version != DATABASE_WRITE_CAPABILITY:
            return
        evidence = (
            await self._session.scalars(
                select(Evidence)
                .join(ToolCall, ToolCall.id == Evidence.tool_call_id)
                .where(
                    Evidence.run_id == binding.run_id,
                    Evidence.evidence_ref.in_(draft.evidence_refs),
                    Evidence.evidence_type == "database",
                    ToolCall.run_id == binding.run_id,
                    ToolCall.integration_id == binding.integration_id,
                    ToolCall.provider == "postgres",
                    ToolCall.capability_version == "database.read/v1",
                    ToolCall.status == "SUCCEEDED",
                )
            )
        ).all()
        if binding.integration_id is None or not any(
            item.metadata_json.get("binding_checksum") == binding.checksum
            and database_observation_matches(
                item.source_locator, payload, integration_id=binding.integration_id
            )
            for item in evidence
        ):
            raise ChangeProposalValidationError("Database proposal requires exact read Evidence")

    async def _validate_effect_binding(
        self, *, run: Run, binding: ResourceBinding, capability_version: str,
        allow_legacy_document_read: bool = False,
    ) -> Integration | None:
        """提案/批准/claim/段階認可で同じ束縛を検証し、文書庫だけを内部資源として扱う。"""

        if capability_version == DOCUMENT_WRITE_CAPABILITY:
            if self._document_library_target is None:
                raise ChangeProposalValidationError("Document library is not configured")
            try:
                DocumentLibraryBindingRepository(
                    self._session, target=self._document_library_target
                ).validate(
                    binding, project_id=run.project_id, run_id=run.id,
                    requirement_key=binding.requirement_key,
                    allow_legacy_read=allow_legacy_document_read,
                )
                frozen = parse_document_library_source(
                    run.selected_sources_json[binding.requirement_key],
                    project_id=run.project_id, run_id=run.id,
                    requirement_key=binding.requirement_key,
                )
                if (
                    frozen.binding_id != binding.id
                    or frozen.target != self._document_library_target
                    or frozen.revision != binding.revision
                    or frozen.to_json()["binding_checksum"] != binding.checksum
                ):
                    raise ValueError("Original document library binding changed")
            except (ValueError, TypeError, KeyError) as error:
                raise ChangeProposalValidationError("Document library binding changed") from error
            return None
        if (
            binding.integration_id is None or binding.run_id != run.id
            or binding.project_id != run.project_id
            or binding.scope_level != ResourceBindingLevel.RUN.value
            or binding.scope_key != str(run.id) or binding.disabled_at is not None
            or binding.capability_version != capability_version
        ):
            raise ChangeProposalValidationError("Frozen Integration binding is invalid")
        expected = binding_checksum(
            project_id=binding.project_id, scope_level=ResourceBindingLevel.RUN,
            scope_key=binding.scope_key, requirement_key=binding.requirement_key,
            resource_kind=binding.resource_kind, integration_id=binding.integration_id,
            provider=binding.provider, capability_version=binding.capability_version,
            revision=binding.revision, scope=dict(binding.scope_json),
        )
        integration = await self._session.get(Integration, binding.integration_id)
        if (
            integration is None or integration.project_id != run.project_id
            or integration.status != IntegrationStatus.ACTIVE.value
            or str(integration.revision) != binding.revision
            or integration.provider != binding.provider
            or capability_version not in integration.capabilities_json
            or binding.checksum != expected
        ):
            raise ChangeProposalValidationError("Integration changed after Run binding")
        return integration

    async def _validate_effect_artifact(
        self, *, run: Run, draft: ChangeProposalDraft, payload: dict[str, Any]
    ) -> None:
        """文書保存では原 Run の検証済み byte を必須とし、提案の自己申告だけで批准しない。"""

        if draft.capability_version != DOCUMENT_WRITE_CAPABILITY:
            return
        try:
            artifact = await ArtifactRepository(self._session).get_content(
                project_id=run.project_id, run_id=run.id, artifact_ref=payload["artifact_ref"]
            )
            if artifact is None or any(
                getattr(artifact.metadata, name) != payload[key]
                for name, key in (
                    ("checksum", "content_hash"), ("size_bytes", "size_bytes"),
                )
            ):
                raise ValueError("Original Artifact does not match the proposed content")
            # Artifact は現行 producer の text/plain。保存 MIME は批准した配信形式であり、
            # Markdown/JSON へのラベル指定で元 byte を変換・置換しない。
        except ValueError as error:
            raise ChangeProposalValidationError(
                "Document Artifact is unavailable or changed"
            ) from error

    async def _validate_proposal_draft(
        self,
        claimed: ClaimedRun,
        *,
        run: Run,
        draft: ChangeProposalDraft,
    ) -> tuple[dict[str, Any], ResourceBinding, Integration | None, dict[str, Any]]:
        """Blueprint intent、Run binding、Integration scope と Provider payload を再検証する。"""

        existing = (
            await self._session.scalars(
                select(ChangeProposal).where(
                    ChangeProposal.run_id == run.id,
                    ChangeProposal.idempotency_key == draft.idempotency_key,
                )
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_fingerprint == draft.request_fingerprint:
                raise ChangeProposalConflictError("ChangeProposal has already been recorded")
            raise ChangeProposalConflictError(
                "ChangeProposal idempotency key was reused with different content"
            )
        if len(claimed.skill_snapshots_json) != 1:
            raise ChangeProposalValidationError("Run has no unique frozen SkillVersion")
        manifest = claimed.skill_snapshots_json[0].get("manifest")
        if not isinstance(manifest, dict):
            raise ChangeProposalValidationError("Run Manifest snapshot is unavailable")
        blueprint = resolve_capability_blueprint(manifest)
        if blueprint is None:
            raise ChangeProposalValidationError("Run CapabilityBlueprint is unavailable")
        intents = {
            str(item["key"]): item
            for item in blueprint.get("effect_intents", [])
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        }
        intent = intents.get(draft.effect_intent_key)
        if intent is None or intent.get("mode") != "apply":
            raise ChangeProposalValidationError(
                "ChangeProposal does not match a declared apply effect intent"
            )
        if intent.get("resource_key") != draft.resource_key:
            raise ChangeProposalValidationError("ChangeProposal targets another resource")
        if intent.get("operation") != draft.operation:
            raise ChangeProposalValidationError("ChangeProposal operation changed from Blueprint")
        declared_risk = str(intent.get("risk", "")).upper()
        if declared_risk != draft.risk_level.value:
            raise ChangeProposalValidationError("ChangeProposal risk changed from Blueprint")
        binding = (
            await self._session.scalars(
                select(ResourceBinding).where(
                    ResourceBinding.run_id == run.id,
                    ResourceBinding.project_id == run.project_id,
                    ResourceBinding.scope_level == ResourceBindingLevel.RUN.value,
                    ResourceBinding.requirement_key == draft.resource_key,
                )
            )
        ).one_or_none()
        if binding is None:
            raise ChangeProposalValidationError("ChangeProposal has no frozen resource binding")
        integration = await self._validate_effect_binding(
            run=run, binding=binding, capability_version=draft.capability_version
        )
        capability = resolve_effect_capability(draft.capability_version)
        if binding.provider not in capability.providers:
            raise ChangeProposalValidationError("Effect capability does not match the Provider")
        provider_payload = capability.validate(
            draft, dict(binding.scope_json),
            dict(integration.config_json) if integration is not None else {}
        )
        await self._validate_database_observation(draft, binding=binding, payload=provider_payload)
        await self._validate_effect_artifact(run=run, draft=draft, payload=provider_payload)
        return intent, binding, integration, provider_payload

    @staticmethod
    def _new_effect_execution(
        *,
        proposal: ChangeProposal,
        approval: ChangeApproval,
        provider: str,
        now: datetime,
    ) -> EffectExecution:
        """Approved Proposal から一意な REQUESTED EffectExecution を作成する。"""

        capability = resolve_effect_capability(proposal.capability_version)
        if provider not in capability.providers:
            raise ChangeProposalValidationError("Effect Provider is not registered")
        return EffectExecution(
            id=uuid4(),
            proposal_id=proposal.id,
            run_id=proposal.run_id,
            approval_id=approval.id,
            tool_call_id=None,
            status=EffectExecutionStatus.REQUESTED.value,
            provider=provider,
            provider_version=capability.provider_versions[provider],
            idempotency_key=proposal.idempotency_key,
            request_fingerprint=proposal.request_fingerprint,
            before_ref=None,
            after_ref=None,
            verification_json={},
            error_json=None,
            worker_id=None,
            lease_token_hash=None,
            lease_expires_at=None,
            heartbeat_at=None,
            attempt_no=0,
            started_at=None,
            executed_at=None,
            created_at=now,
            updated_at=now,
        )

    async def _validate_proposal_row(
        self, proposal: ChangeProposal, *, run: Run,
        allow_legacy_document_read: bool = False,
    ) -> dict[str, Any]:
        """Approval 時に Proposal と binding、Integration、Evidence ownership を再検証する。"""

        binding = await self._session.get(ResourceBinding, proposal.target_binding_id)
        if binding is None or binding.integration_id != proposal.integration_id:
            raise ChangeProposalValidationError("Proposal target changed after creation")
        integration = await self._validate_effect_binding(
            run=run, binding=binding, capability_version=proposal.capability_version,
            allow_legacy_document_read=allow_legacy_document_read,
        )
        changes = proposal.preview_json.get("changes")
        if not isinstance(changes, list) or not all(isinstance(item, dict) for item in changes):
            raise ChangeProposalValidationError("Proposal preview snapshot is invalid")
        content = {
            "project_id": str(proposal.project_id),
            "run_id": str(proposal.run_id),
            "run_segment_id": str(proposal.run_segment_id),
            "agent_session_id": str(proposal.agent_session_id),
            "skill_version_id": str(proposal.skill_version_id),
            "target_binding_id": str(proposal.target_binding_id),
            "integration_id": (
                str(proposal.integration_id) if proposal.integration_id is not None else None
            ),
            "effect_intent_key": proposal.effect_intent_key,
            "resource_key": binding.requirement_key,
            "capability_version": proposal.capability_version,
            "operation": proposal.operation,
            "target": proposal.target_json,
            "changes": changes,
            "precondition": proposal.precondition_json,
            "summary": proposal.summary,
            "evidence_refs": proposal.evidence_refs_json,
            "risk_level": proposal.risk_level,
            "reversible": proposal.reversible,
            "rollback": proposal.rollback_json,
            "verification": proposal.verification_json,
            "idempotency_key": proposal.idempotency_key,
            "request_fingerprint": proposal.request_fingerprint,
            "expires_at": proposal.expires_at.isoformat(),
            "version": proposal.version,
        }
        if proposal.checksum != proposal_checksum(content):
            raise ChangeProposalValidationError("Proposal checksum does not match stored content")
        draft = ChangeProposalDraft(
            effect_intent_key=proposal.effect_intent_key,
            resource_key=binding.requirement_key,
            capability_version=proposal.capability_version,
            operation=proposal.operation,
            target=dict(proposal.target_json),
            changes=tuple(dict(item) for item in changes),
            precondition=dict(proposal.precondition_json),
            summary=proposal.summary,
            evidence_refs=tuple(proposal.evidence_refs_json),
            risk_level=EffectRiskLevel(proposal.risk_level),
            reversible=proposal.reversible,
            rollback=dict(proposal.rollback_json),
            verification=dict(proposal.verification_json),
            idempotency_key=proposal.idempotency_key,
            request_fingerprint=proposal.request_fingerprint,
            expires_at=proposal.expires_at,
            continuation_mode=proposal.continuation_mode,
            checkpoint=dict(proposal.checkpoint_json),
            checkpoint_checksum=proposal.checkpoint_checksum,
        )
        payload = resolve_effect_capability(proposal.capability_version).validate(
            draft, dict(binding.scope_json),
            dict(integration.config_json) if integration is not None else {}
        )
        await self._validate_database_observation(draft, binding=binding, payload=payload)
        await self._validate_effect_artifact(run=run, draft=draft, payload=payload)
        await self._validate_evidence_refs(run.id, tuple(proposal.evidence_refs_json))
        return payload

    @staticmethod
    def _next_effect_segment(
        *,
        run: Run,
        segment: RunSegment,
        proposal: ChangeProposal,
        trigger_ref: UUID,
        outcome: str,
        now: datetime,
        evidence_refs: tuple[str, ...] = (),
        effect_result: dict[str, Any] | None = None,
        trigger_type: RunSegmentTrigger = RunSegmentTrigger.APPROVAL_RESPONSE,
    ) -> RunSegment:
        """Effect outcome を checkpoint へ追加し、同じ Run の次 Segment を作成する。"""

        checkpoint = dict(proposal.checkpoint_json)
        # 前回の回执や入力由来の同名値を、今回の結果として引き継がない。
        checkpoint.pop("effect_result", None)
        if effect_result is not None:
            if outcome != "APPLIED":
                raise ValueError("Only an applied Effect can supply a continuation result")
            checkpoint["effect_result"] = validated_effect_result(effect_result)
        facts = [
            str(item)
            for item in checkpoint.get("confirmed_facts", [])
            if isinstance(item, str)
        ]
        facts.append(
            f"ChangeProposal {proposal.proposal_ref} reached controlled effect outcome {outcome}."
        )
        checkpoint["confirmed_facts"] = facts
        merged_evidence = [
            str(item)
            for item in checkpoint.get("evidence_refs", [])
            if isinstance(item, str)
        ]
        for evidence_ref in evidence_refs:
            if evidence_ref not in merged_evidence:
                merged_evidence.append(evidence_ref)
        checkpoint["evidence_refs"] = merged_evidence
        return RunSegment(
            id=uuid4(),
            run_id=run.id,
            segment_no=segment.segment_no + 1,
            trigger_type=trigger_type.value,
            trigger_ref=trigger_ref,
            status=RunSegmentStatus.CREATED.value,
            objective_json={
                "text": (
                    f"Continue the Run after ChangeProposal {proposal.proposal_ref} "
                    f"finished with outcome {outcome}."
                )
            },
            checkpoint_json=checkpoint,
            continuation_mode=proposal.continuation_mode,
            parent_agent_session_id=proposal.agent_session_id,
            instruction_snapshot_id=None,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _stored_change_proposal(row: ChangeProposal) -> StoredChangeProposal:
        """ChangeProposal ORM row を公開 read model へ変換する。"""

        changes = row.preview_json.get("changes", [])
        return StoredChangeProposal(
            proposal_id=row.id,
            proposal_ref=row.proposal_ref,
            project_id=row.project_id,
            run_id=row.run_id,
            run_segment_id=row.run_segment_id,
            agent_session_id=row.agent_session_id,
            target_binding_id=row.target_binding_id,
            integration_id=row.integration_id,
            effect_intent_key=row.effect_intent_key,
            capability_version=row.capability_version,
            operation=row.operation,
            target=dict(row.target_json),
            summary=row.summary,
            changes=(
                tuple(dict(item) for item in changes if isinstance(item, dict))
                if isinstance(changes, list)
                else ()
            ),
            precondition=dict(row.precondition_json),
            evidence_refs=tuple(row.evidence_refs_json),
            risk_level=EffectRiskLevel(row.risk_level),
            reversible=row.reversible,
            rollback=dict(row.rollback_json),
            verification=dict(row.verification_json),
            continuation_mode=row.continuation_mode,
            checkpoint=dict(row.checkpoint_json),
            checkpoint_checksum=row.checkpoint_checksum,
            idempotency_key=row.idempotency_key,
            status=ChangeProposalStatus(row.status),
            version=row.version,
            checksum=row.checksum,
            expires_at=row.expires_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _stored_change_approval(row: ChangeApproval) -> StoredChangeApproval:
        """ChangeApproval ORM row を公開 read model へ変換する。"""

        return StoredChangeApproval(
            approval_id=row.id,
            proposal_id=row.proposal_id,
            run_id=row.run_id,
            source=ApprovalSource(row.source),
            decision=ApprovalDecision(row.decision),
            actor_id=row.actor_id,
            preauthorization_id=row.preauthorization_id,
            proposal_version=row.proposal_version,
            proposal_checksum=row.proposal_checksum,
            reason=row.reason,
            created_at=row.created_at,
        )

    @staticmethod
    def _stored_effect_execution(row: EffectExecution) -> StoredEffectExecution:
        """EffectExecution ORM row を lease/credential なしの公開 read model へ変換する。"""

        return StoredEffectExecution(
            effect_execution_id=row.id,
            proposal_id=row.proposal_id,
            run_id=row.run_id,
            approval_id=row.approval_id,
            tool_call_id=row.tool_call_id,
            status=EffectExecutionStatus(row.status),
            provider=row.provider,
            provider_version=row.provider_version,
            before_ref=row.before_ref,
            after_ref=row.after_ref,
            verification=dict(row.verification_json),
            error=dict(row.error_json) if row.error_json is not None else None,
            attempt_no=row.attempt_no,
            executed_at=row.executed_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def recover_expired_effects(
        self, *, now: datetime, limit: int, max_attempts: int
    ) -> int:
        """期限切れ effect lease を再 dispatch し、取消・expiry・上限超過は終局化する。"""

        if limit <= 0 or max_attempts <= 0:
            raise ValueError("Effect recovery limits must be positive")
        candidates = list(
            (
                await self._session.scalars(
                    select(EffectExecution)
                    .join(ChangeProposal, ChangeProposal.id == EffectExecution.proposal_id)
                    .where(
                        ChangeProposal.capability_version.in_(self._execution_features.write_capabilities),
                        or_(ChangeProposal.capability_version != DATABASE_WRITE_CAPABILITY,
                            ChangeProposal.operation.in_(["INSERT", "UPDATE"])),
                        EffectExecution.status.in_(
                            {
                                EffectExecutionStatus.LEASED.value,
                                EffectExecutionStatus.APPLYING.value,
                            }
                        ),
                        EffectExecution.lease_expires_at.is_not(None),
                        EffectExecution.lease_expires_at <= now,
                    )
                    .order_by(EffectExecution.lease_expires_at, EffectExecution.id)
                    .limit(limit)
                )
            ).all()
        )
        recovered = 0
        for candidate in candidates:
            observed_proposal = await self._session.get(
                ChangeProposal, candidate.proposal_id
            )
            if observed_proposal is None:
                raise ChangeProposalNotFoundError("Effect recovery Proposal not found")
            # Executor と同じ Run → Segment → Proposal → Effect の row lock 順を守る。
            run = await self._lock_run_row(candidate.run_id)
            if run is None:
                raise RunNotFoundError("Effect recovery Run not found")
            segment = await self._lock_segment_row(
                observed_proposal.run_segment_id, run_id=run.id
            )
            if segment is None:
                raise RunNotFoundError("Effect recovery Segment not found")
            proposal = (
                await self._session.scalars(
                    select(ChangeProposal)
                    .where(ChangeProposal.id == observed_proposal.id)
                    .with_for_update()
                )
            ).one()
            execution = (
                await self._session.scalars(
                    select(EffectExecution)
                    .where(EffectExecution.id == candidate.id)
                    .with_for_update()
                )
            ).one()
            if not self._execution_features.effect_enabled(
                proposal.capability_version, proposal.operation
            ):
                continue
            if (
                execution.status
                not in {
                    EffectExecutionStatus.LEASED.value,
                    EffectExecutionStatus.APPLYING.value,
                }
                or execution.lease_expires_at is None
                or execution.lease_expires_at > now
            ):
                # Candidate 取得後に別 Worker が finalize/recover 済みなら何もしない。
                continue
            if RunStatus(run.status) is not RunStatus.WAITING_FOR_APPROVAL:
                execution.status = EffectExecutionStatus.FAILED.value
                execution.error_json = effect_failure_record(
                    capability=proposal.capability_version, attempt_no=execution.attempt_no,
                    code="run_left_effect_waiting_state", retryable=False,
                    previous=execution.error_json,
                )
                execution.worker_id = None
                execution.lease_token_hash = None
                execution.lease_expires_at = None
                execution.executed_at = now
                execution.updated_at = now
                proposal.status = ChangeProposalStatus.FAILED.value
                proposal.updated_at = now
                if (effect_requires_reconciliation(execution.error_json)
                    and execution.tool_call_id is not None):
                    tool_call = await self._session.get(
                        ToolCall, execution.tool_call_id, with_for_update=True,
                    )
                    if tool_call is not None:
                        tool_call.status = "FAILED"
                        tool_call.result_json = None
                        tool_call.error_json = _effect_tool_error(
                            code=UNKNOWN_EFFECT_CODE, retryable=False,
                        )
                        tool_call.updated_at = now
                    # Run の終局 snapshot 後に event を加えない。原 Effect/Tool の更新は
                    # 履歴 detail の再読取で確認でき、Run の結果を上書きしない。
                recovered += 1
                continue
            if await self.is_cancellation_requested(run.id):
                await self._close_effect_before_provider(
                    run=run,
                    segment=segment,
                    proposal=proposal,
                    execution=execution,
                    status=EffectExecutionStatus.FAILED,
                    code="run_cancelled",
                    now=now,
                )
            elif proposal.expires_at <= now:
                await self._close_effect_before_provider(
                    run=run,
                    segment=segment,
                    proposal=proposal,
                    execution=execution,
                    status=EffectExecutionStatus.STALE,
                    code="proposal_expired",
                    now=now,
                )
            elif execution.attempt_no >= max_attempts:
                await self._close_effect_before_provider(
                    run=run,
                    segment=segment,
                    proposal=proposal,
                    execution=execution,
                    status=EffectExecutionStatus.FAILED,
                    code="retry_exhausted",
                    now=now,
                )
            else:
                execution.status = EffectExecutionStatus.REQUESTED.value
                execution.worker_id = None
                execution.lease_token_hash = None
                execution.lease_expires_at = None
                execution.heartbeat_at = now
                execution.error_json = effect_failure_record(
                    capability=proposal.capability_version, attempt_no=execution.attempt_no,
                    code="lease_expired", retryable=True, previous=execution.error_json,
                )
                execution.updated_at = now
                proposal.status = ChangeProposalStatus.APPROVED.value
                proposal.updated_at = now
                if execution.tool_call_id is not None:
                    tool_call = await self._session.get(
                        ToolCall, execution.tool_call_id, with_for_update=True
                    )
                    if tool_call is not None:
                        tool_call.status = "FAILED"
                        tool_call.result_json = None
                        tool_call.error_json = _effect_tool_error(
                            code=execution.error_json["code"],
                            retryable=True,
                        )
                        tool_call.updated_at = now
                self._session.add(
                    self._effect_dispatch_outbox(execution, occurred_at=now)
                )
            recovered += 1
        return recovered

    async def recover_expired_proposals(self, *, now: datetime, limit: int) -> int:
        """未回答の effect approval を期限で閉じ、同じ Run を次 Segment へ進める。"""

        if limit <= 0:
            raise ValueError("Proposal recovery limit must be positive")
        candidates = list(
            (
                await self._session.scalars(
                    select(ChangeProposal)
                    .where(
                        ChangeProposal.capability_version.in_(self._execution_features.write_capabilities),
                        or_(ChangeProposal.capability_version != DATABASE_WRITE_CAPABILITY,
                            ChangeProposal.operation.in_(["INSERT", "UPDATE"])),
                        ChangeProposal.status
                        == ChangeProposalStatus.PENDING_APPROVAL.value,
                        ChangeProposal.expires_at <= now,
                    )
                    .order_by(ChangeProposal.expires_at, ChangeProposal.id)
                    .limit(limit)
                )
            ).all()
        )
        recovered = 0
        for candidate in candidates:
            # Worker claim と同じく identity を先に観測し、lock は Run → Segment → Proposal
            # → Interaction の順で取得する。
            run = await self._lock_run_row(candidate.run_id)
            if run is None:
                raise RunNotFoundError("Proposal recovery Run not found")
            segment = await self._lock_segment_row(candidate.run_segment_id, run_id=run.id)
            if segment is None:
                raise RunNotFoundError("Proposal recovery Segment not found")
            proposal = (
                await self._session.scalars(
                    select(ChangeProposal)
                    .where(ChangeProposal.id == candidate.id)
                    .with_for_update()
                )
            ).one()
            if not self._execution_features.effect_enabled(
                proposal.capability_version, proposal.operation
            ):
                continue
            if (
                proposal.status != ChangeProposalStatus.PENDING_APPROVAL.value
                or proposal.expires_at > now
            ):
                continue
            interaction = (
                await self._session.scalars(
                    select(UserInteraction)
                    .where(
                        UserInteraction.change_proposal_id == proposal.id,
                        UserInteraction.run_id == run.id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if interaction is None:
                raise ChangeProposalValidationError(
                    "Expired ChangeProposal has no approval interaction"
                )
            if (
                RunStatus(run.status) is RunStatus.WAITING_FOR_APPROVAL
                and interaction.status == UserInteractionStatus.OPEN.value
            ):
                await self._expire_pending_proposal(
                    run=run,
                    segment=segment,
                    proposal=proposal,
                    interaction=interaction,
                    now=now,
                    trace_id=None,
                )
            else:
                # Run が既に terminal/continuation 済みでも、古い approval を再利用できない
                # status へ閉じる。終態 Run へ新 event や Segment は追加しない。
                proposal.status = ChangeProposalStatus.STALE.value
                proposal.updated_at = now
                if interaction.status == UserInteractionStatus.OPEN.value:
                    interaction.status = UserInteractionStatus.EXPIRED.value
                    interaction.version += 1
                    interaction.updated_at = now
            recovered += 1
        return recovered


def _effect_tool_error(*, code: str, retryable: bool) -> dict[str, Any]:
    """Tool contract に適合し、外部 response 本文を含まない effect error を返す。"""

    return {
        "status": "error",
        "code": code,
        "message": (
            "Original external write result requires reconciliation"
            if code == UNKNOWN_EFFECT_CODE else "Controlled effect could not be completed"
        ),
        "retryable": retryable,
    }
