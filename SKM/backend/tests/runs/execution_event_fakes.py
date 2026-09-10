"""取消競争の検証で、同じ合法 event を repository と Worker の両方へ渡す。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.effects.proposal import parse_change_proposal_request
from skillmind.runs.domain import AgentSessionMetadata, ClaimedRun
from skillmind.runs.interaction import parse_interaction_request
from skillmind.runs.repository import RunRepository


def execution_event(claimed: ClaimedRun, kind: AgentEventType, *, sequence: int = 10) -> AgentEvent:
    """外部接続を要求せず、実 parser を通る質問/提案と観測用量を作る。"""

    checkpoint = {
        "summary": "A finding is ready.",
        "confirmed_facts": [],
        "evidence_refs": [],
        "artifact_refs": [],
        "change_proposal_refs": [],
    }
    payload: dict[str, Any] = {"usage": {"input_tokens": 12}, "total_cost_usd": 0.02}
    if kind is AgentEventType.INTERACTION_REQUESTED:
        payload["interaction_request"] = {
            "interaction_type": "REVIEW",
            "prompt": "Review the finding.",
            "rationale": "Project context is needed.",
            "impact": "The recommendation may change.",
            "options": [],
            "allow_multiple": False,
            "required": True,
            "expires_in_seconds": 3600,
            "continuation_mode": "FORK",
            "checkpoint": checkpoint,
        }
    elif kind is AgentEventType.CHANGE_PROPOSED:
        payload["change_proposal_request"] = {
            "effect_intent_key": "update-issue",
            "resource_key": "issues",
            "capability_version": "issue.update/v1",
            "operation": "update_fields",
            "target": {"locator": "1001", "display": "Example issue"},
            "changes": [{"path": "/fields/status_id", "action": "SET", "value": "3"}],
            "precondition": {"revision": "fixture-revision"},
            "summary": "Update an issue field.",
            "evidence_refs": ["ev_fixture_1"],
            "risk_level": "LOW",
            "reversible": True,
            "rollback": {"description": "Restore the original field."},
            "verification": {"method": "READ_BACK", "paths": ["/fields/status_id"]},
            "idempotency_key": "proposal-fixture-001",
            "expires_in_seconds": 3600,
            "continuation_mode": "RESUME",
            "checkpoint": checkpoint,
        }
    return AgentEvent(
        run_id=claimed.run_id,
        run_attempt_id=claimed.run_attempt_id,
        agent_session_id=str(uuid4()),
        sequence=sequence,
        occurred_at=datetime.now(UTC),
        event_type=kind,
        payload=payload,
    )


async def persist_execution_event(
    repository: RunRepository,
    claimed: ClaimedRun,
    event: AgentEvent,
    metadata: AgentSessionMetadata,
) -> UUID | None:
    """同じ監査境界を通る三つの実 repository entry point を検証へ公開する。"""

    if event.event_type is AgentEventType.INTERACTION_REQUESTED:
        return await repository.suspend_for_interaction(
            claimed,
            event=event,
            session_metadata=metadata,
            request=parse_interaction_request(event.payload["interaction_request"]),
        )
    if event.event_type is AgentEventType.CHANGE_PROPOSED:
        return await repository.suspend_for_proposal(
            claimed,
            event=event,
            session_metadata=metadata,
            draft=parse_change_proposal_request(event.payload["change_proposal_request"]),
        )
    await repository.append_agent_event(claimed, event, session_metadata=metadata)
    return None
