"""実 Service と Repository を通し、直接交付の採番・保存・原同意を検証する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import AgentSession, ChangeApproval, ChangeProposal, EffectExecution
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.domain import RunCancellationRequestedError
from skillmind.runs.repository import RunRepository
from skillmind.runs.service import RunService
from tests.runs.test_execution_gates import execution_rows
from tests.runs.test_run_start_approval import freeze_consent


@pytest.mark.parametrize("mode", ["automatic", "manual", "cancelled"])
async def test_service_starts_inline_proposal_with_locked_sequence(monkeypatch, mode):
    """sequence=0 を作らず、原同意がある場合だけ同じ TX で提案・批准・Effect を保存する。"""
    claimed, run, segment, attempt = execution_rows()
    run.permission_snapshot_json = {"actor_id": str(claimed.actor_id)}
    run.task_snapshot_json = {"skill_version_id": str(uuid4())}
    segment.checkpoint_json = {}
    freeze_consent(run, enabled=mode != "manual")
    sdk_session = uuid4()
    agent = AgentSession(id=uuid4(), sdk_session_id=sdk_session)
    binding = SimpleNamespace(id=uuid4(), integration_id=uuid4(), provider="postgres")
    arguments = {
        "resource_key": "records", "capability_version": "database.write/v1",
        "operation": "INSERT", "target": {"locator": "public.reports", "display": "Report"},
        "changes": [{"path": "/row", "action": "SET", "value": {
            "key": {"id": "fixture"}, "values": {"status": "RUNNING"}, "expected": None,
        }}],
        "precondition": {"revision": "absent"}, "summary": "Register execution.",
        "evidence_refs": ["ev_original"],
    }
    state = SimpleNamespace(transaction=False, locked=False, committed=False, numbered=False)
    rows = []
    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    session.add.side_effect = rows.append

    @asynccontextmanager
    async def transaction():
        """保存完了後だけ commit を観測し、終了後の返却を確認する。"""
        state.transaction = True
        try:
            yield
            state.committed = True
        finally:
            state.transaction = False

    session.begin.side_effect = transaction

    async def lock(_claimed):
        """DB lock の I/O だけ置換し、採番は lock 後であることを確認する。"""
        assert state.transaction and _claimed == claimed
        state.locked = True
        return run, segment, attempt

    async def scalar(statement):
        """実際の採番 SQL と session/effect 検索を、同じ保存済み行へ解決する。"""
        assert state.transaction
        if "max(run_events.sequence)" in str(statement):
            assert state.locked
            state.numbered = True
            return 42
        entity = statement.column_descriptions[0].get("entity")
        if entity is AgentSession:
            return agent
        if entity is EffectExecution:
            return next(row.id for row in rows if isinstance(row, EffectExecution))
        return None

    session.scalar.side_effect = scalar
    repository = RunRepository(session, execution_features=ExecutionFeatures(database_writes=True))
    monkeypatch.setattr(repository, "_lock_claimed_execution", lock)
    monkeypatch.setattr(repository, "_validate_proposal_draft", AsyncMock(return_value=(
        {}, binding, None, {"table": "public.reports", "operation": "INSERT",
                           "key": {"id": "fixture"}, "values": {"status": "RUNNING"}},
    )))
    monkeypatch.setattr(repository, "_validate_checkpoint_refs", AsyncMock())
    monkeypatch.setattr(repository, "_validate_evidence_refs", AsyncMock())
    validate_event = MagicMock(wraps=repository._validate_agent_event)
    monkeypatch.setattr(repository, "_validate_agent_event", validate_event)
    if mode == "cancelled":
        monkeypatch.setattr(repository, "_reject_cancelled_execution", AsyncMock(
            side_effect=RunCancellationRequestedError("cancelled"),
        ))
    monkeypatch.setattr("skillmind.runs.service.RunRepository", lambda *a, **kw: repository)
    service = RunService(MagicMock(return_value=session), database_writes_enabled=True)
    call = service.begin_inline_effect(
        claimed, arguments=arguments, tool_use_id="call-original", session_id=str(sdk_session),
    )
    if mode == "cancelled":
        with pytest.raises(RunCancellationRequestedError):
            await call
        assert not state.committed and not state.numbered and not rows
        return
    result = await call
    event = validate_event.call_args.args[0]
    assert state.numbered and event.sequence == 42
    assert event.agent_session_id == str(sdk_session)
    assert event.run_id == claimed.run_id and event.run_attempt_id == claimed.run_attempt_id
    if mode == "manual":
        assert result is None and not state.committed and not rows
        return
    assert state.committed and not state.transaction
    proposal, approval, effect = rows
    assert isinstance(proposal, ChangeProposal) and isinstance(approval, ChangeApproval)
    assert isinstance(effect, EffectExecution)
    assert result == (proposal.id, effect.id)
    assert approval.source == "RUN_START" and approval.proposal_id == proposal.id
    assert effect.approval_id == approval.id and effect.proposal_id == proposal.id
    assert proposal.inline_owner_json["tool_use_id"] == "call-original"
    assert attempt.inline_proposal_id == proposal.id
    assert run.status == segment.status == attempt.status == "RUNNING"
    session.add_all.assert_not_called()
