"""Artifact 読取を挟む待機保存で、原候補と lease の観測時点を守る。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.agent.domain import AgentEventType
from projectmind.artifacts.repository import ArtifactRepository
from projectmind.db.models import Run, RunAttempt, RunSegment
from projectmind.effects.domain import ChangeProposalDraft
from projectmind.effects.proposal import parse_change_proposal_request
from projectmind.runs.domain import ClaimedRun, LeaseValidationError
from projectmind.runs.interaction import InteractionRequestDraft, parse_interaction_request
from projectmind.runs.repository import RunRepository
from tests.runs.execution_event_fakes import execution_event
from tests.runs.test_execution_gates import execution_rows


class CheckpointObserved(RuntimeError):
    """書込前の観測点で試験を止める sentinel。"""


@pytest.mark.parametrize(
    "kind", [AgentEventType.INTERACTION_REQUESTED, AgentEventType.CHANGE_PROPOSED]
)
@pytest.mark.parametrize("expired", [False, True])
async def test_wait_keeps_original_checkpoint_and_rechecks_lease_after_artifact_read(
    monkeypatch: pytest.MonkeyPatch,
    kind: AgentEventType,
    expired: bool,
) -> None:
    """先頭 await で元 DTO を変えても参照が増えず、遅い lookup 後の期限切れでは保存しない。"""

    claimed, run, segment, attempt = execution_rows()
    event = execution_event(claimed, kind)
    key = (
        "interaction_request"
        if kind is AgentEventType.INTERACTION_REQUESTED
        else "change_proposal_request"
    )
    payload = event.payload[key]
    payload["checkpoint"]["artifact_refs"] = ["art_original"]
    draft = (
        parse_interaction_request(payload)
        if kind is AgentEventType.INTERACTION_REQUESTED
        else parse_change_proposal_request(payload)
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)
    repository = RunRepository(session)

    async def lock(_claimed: ClaimedRun) -> tuple[Run, RunSegment, RunAttempt]:
        """呼出元に残る nested list を、repository が待機に入った後で改変する。"""

        draft.checkpoint["artifact_refs"].append("art_injected")
        return run, segment, attempt

    monkeypatch.setattr(repository, "_lock_claimed_execution", lock)
    monkeypatch.setattr(repository, "_next_sequence", AsyncMock(return_value=1))
    monkeypatch.setattr(repository, "_validate_proposal_draft", AsyncMock(return_value=(None,) * 4))
    observed: list[frozenset[str]] = []

    async def verify(
        _repository: ArtifactRepository, run_id: UUID, refs: frozenset[str],
    ) -> frozenset[str]:
        """実際に共有 verifier に届いた候補を記録し、期限通過を原 Attempt に反映する。"""

        assert run_id == claimed.run_id
        observed.append(refs)
        if not expired:
            raise CheckpointObserved
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return refs

    monkeypatch.setattr("projectmind.artifacts.repository.ArtifactRepository.verified_refs", verify)
    ensure = AsyncMock(side_effect=AssertionError("No Session may be saved after this gate"))
    monkeypatch.setattr(repository, "_ensure_agent_session", ensure)
    with pytest.raises(LeaseValidationError if expired else CheckpointObserved):
        if kind is AgentEventType.INTERACTION_REQUESTED:
            assert isinstance(draft, InteractionRequestDraft)
            await repository.suspend_for_interaction(
                claimed,
                event=event,
                session_metadata=MagicMock(),
                request=draft,
            )
        else:
            assert isinstance(draft, ChangeProposalDraft)
            await repository.suspend_for_proposal(
                claimed,
                event=event,
                session_metadata=MagicMock(),
                draft=draft,
            )
    assert observed == [frozenset({"art_original"})]
    ensure.assert_not_awaited()
    session.add_all.assert_not_called()
