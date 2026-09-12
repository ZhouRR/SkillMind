"""原 Proposal を処理した Segment だけに、SDK control 呼出しの読取完了を渡す。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    AgentSession,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    RunSegment,
    UserInteraction,
)
from skillmind.effects.continuation import validated_effect_result
from skillmind.effects.outcomes import effect_requires_reconciliation
from skillmind.runs.domain import ClaimedRun, SessionContinuationMode


@dataclass(frozen=True, slots=True)
class ResolvedProposal:
    """原要求の完全一致だけに使う内部記述子。Agent の入力からは生成しない。"""

    request_fingerprint: str
    sdk_session_id: str
    proposal_ref: str
    outcome: str
    tool_use_id: str | None = None

    def matches(self, arguments: Mapping[str, Any], session_id: str) -> bool:
        """同じ SDK Session と元の JSON 要求を照合し、別提案を許可しない。"""
        return (
            session_id == self.sdk_session_id
            and sha256_hex(canonical_json(dict(arguments))) == self.request_fingerprint
        )


class ProposalContinuationReader:
    """新 Segment の trigger と永続 outcome を結び、外部 Provider を呼ばず確認する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Worker と同じアプリケーション DB の短い読取 session を使う。"""
        self._session_factory = session_factory

    async def load(self, claimed: ClaimedRun) -> ResolvedProposal | None:
        """承認待ち・別 Session・別 Segment を確定済みとして返さない。"""
        if (
            claimed.continuation_mode is not SessionContinuationMode.RESUME
            or claimed.run_segment_id is None
            or claimed.parent_agent_session_id is None
            or claimed.parent_sdk_session_id is None
        ):
            return None
        async with asyncio.timeout(20), self._session_factory() as session:
            segment = await session.get(RunSegment, claimed.run_segment_id)
            parent = await session.get(AgentSession, claimed.parent_agent_session_id)
            if segment is None or parent is None:
                raise ValueError("Proposal continuation lineage is unavailable")
            if (
                segment.run_id != claimed.run_id
                or segment.parent_agent_session_id != parent.id
                or parent.run_id != claimed.run_id
                or parent.sdk_session_id != claimed.parent_sdk_session_id
                or segment.segment_no != claimed.segment_no
                or segment.continuation_mode != claimed.continuation_mode.value
                or segment.checkpoint_json != claimed.checkpoint_json
            ):
                raise ValueError("Proposal continuation lineage changed")
            if segment.trigger_type not in {"APPROVAL_RESPONSE", "APPROVAL_TIMEOUT"}:
                return None
            proposal = (
                await session.scalars(
                    select(ChangeProposal).where(
                        ChangeProposal.run_id == claimed.run_id,
                        ChangeProposal.project_id == claimed.project_id,
                        ChangeProposal.agent_session_id == parent.id,
                        ChangeProposal.run_segment_id == parent.run_segment_id,
                    )
                )
            ).one_or_none()
            if proposal is None:
                raise ValueError("Original continuation proposal is unavailable")
            outcome = await _resolved_outcome(session, segment, proposal)
            return ResolvedProposal(
                request_fingerprint=proposal.request_fingerprint,
                sdk_session_id=str(parent.sdk_session_id),
                proposal_ref=proposal.proposal_ref,
                outcome=outcome,
            )


async def _resolved_outcome(
    session: AsyncSession, segment: RunSegment, proposal: ChangeProposal
) -> str:
    """trigger_ref の原回执だけを採用し、checkpoint の文章から批准を推定しない。"""
    trigger = segment.trigger_ref
    if not isinstance(trigger, UUID):
        raise ValueError("Proposal continuation trigger is missing")
    if segment.trigger_type == "APPROVAL_TIMEOUT":
        interaction = await session.get(UserInteraction, trigger)
        if (
            interaction is None
            or interaction.run_id != proposal.run_id
            or interaction.change_proposal_id != proposal.id
            or interaction.status != "EXPIRED"
            or proposal.status != "STALE"
        ):
            raise ValueError("Original approval expiry is unavailable")
        return "EXPIRED"
    if proposal.status == "REJECTED":
        approval = await session.get(ChangeApproval, trigger)
        if (
            approval is None
            or approval.run_id != proposal.run_id
            or approval.proposal_id != proposal.id
            or approval.decision != "REJECTED"
            or approval.proposal_checksum != proposal.checksum
        ):
            raise ValueError("Original proposal rejection is unavailable")
        return "REJECTED"
    effect = await session.get(EffectExecution, trigger)
    if (
        effect is None
        or effect.run_id != proposal.run_id
        or effect.proposal_id != proposal.id
        or effect.status not in {"APPLIED", "STALE", "FAILED", "VERIFICATION_FAILED"}
        or effect_requires_reconciliation(effect.error_json)
        or proposal.status
        != {
            "APPLIED": "APPLIED",
            "STALE": "STALE",
            "FAILED": "FAILED",
            "VERIFICATION_FAILED": "FAILED",
        }[effect.status]
    ):
        raise ValueError("Original proposal effect is unresolved")
    result = segment.checkpoint_json.get("effect_result")
    if effect.status == "APPLIED":
        if not isinstance(result, Mapping):
            raise ValueError("Applied proposal continuation result is missing")
        checked = validated_effect_result(result)
        if (
            checked["effect_execution_id"] != str(effect.id)
            or checked["proposal_ref"] != proposal.proposal_ref
            or checked["capability_version"] != proposal.capability_version
            or checked["after_ref"] != effect.after_ref
            or ("before_ref" in checked and checked["before_ref"] != effect.before_ref)
        ):
            raise ValueError("Applied proposal continuation result changed")
    elif result is not None:
        raise ValueError("Unapplied proposal cannot carry an applied result")
    return effect.status
