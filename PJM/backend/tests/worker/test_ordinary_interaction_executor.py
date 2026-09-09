"""普通 interaction の三題型と批准用 event の分離を外部 engine なしで検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEventType
from projectmind.runs.domain import RunStatus
from projectmind.worker.executor import AgentRunExecutor
from tests.runs.execution_event_fakes import execution_event
from tests.worker.test_agent_run_executor import ContextBuilder, SequenceEngine, _claimed, _service


@pytest.mark.parametrize(
    ("interaction_type", "duplicate_option"),
    [("CLARIFICATION", False), ("CHOICE", False), ("REVIEW", False),
     ("EFFECT_APPROVAL", False), ("CHOICE", True)],
)
async def test_executor_only_suspends_for_valid_ordinary_requests(
    tmp_path: Path,
    interaction_type: str,
    duplicate_option: bool,
) -> None:
    """旧承認と識別不能な選択肢を失敗終態にし、有効な普通質問の待機と区別する。"""

    claimed = replace(_claimed(), run_segment_id=uuid4(), segment_no=1)
    event = execution_event(claimed, AgentEventType.INTERACTION_REQUESTED)
    request = event.payload["interaction_request"]
    request["interaction_type"] = interaction_type
    if interaction_type == "CHOICE":
        request["options"] = [
            {"key": "same" if duplicate_option else key, "label": key,
             "description": "Review direction.", "recommended": False}
            for key in ("continue", "revise")
        ]
    service = _service()
    service.suspend_for_proposal = AsyncMock()
    await AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([event]),
        result_validator=MagicMock(),
        lease_seconds=60,
    ).execute(claimed)
    service.suspend_for_proposal.assert_not_awaited()
    if interaction_type == "EFFECT_APPROVAL" or duplicate_option:
        service.suspend_for_interaction.assert_not_awaited()
        service.finalize_execution.assert_awaited_once()
        terminal = service.finalize_execution.await_args.kwargs
        assert terminal["target"] is RunStatus.FAILED
        assert terminal["error_json"]["code"] == "interaction_request_invalid"
        assert "interaction_request" not in terminal["error_json"]
    else:
        service.suspend_for_interaction.assert_awaited_once()
        service.finalize_execution.assert_not_awaited()
        assert (
            service.suspend_for_interaction.await_args.kwargs["interaction"].interaction_type.value
            == interaction_type
        )


async def test_legal_change_proposal_keeps_separate_approval_path(tmp_path: Path) -> None:
    """有効 Proposal の批准待ちは普通 interaction 制限の影響を受けない。"""

    claimed = replace(_claimed(), run_segment_id=uuid4(), segment_no=1)
    service = _service()
    service.suspend_for_proposal = AsyncMock(return_value=uuid4())
    await AgentRunExecutor(
        run_service=service,
        context_builder=ContextBuilder(tmp_path),
        engine=SequenceEngine([execution_event(claimed, AgentEventType.CHANGE_PROPOSED)]),
        result_validator=MagicMock(),
        lease_seconds=60,
    ).execute(claimed)
    service.suspend_for_interaction.assert_not_awaited()
    service.suspend_for_proposal.assert_awaited_once()
    service.finalize_execution.assert_not_awaited()
