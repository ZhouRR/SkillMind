"""Run 限定同意の固定、提案分岐、段階撤権を検証する。外部 write は実行しない。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.domain import AgentEventType
from skillmind.db.models import (
    AgentSession,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    UserInteraction,
)
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.domain import EffectLeaseValidationError
from skillmind.effects.proposal import parse_change_proposal_request
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.run_approval import run_auto_approval_actor
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.creation_replay import validate_creation_replay
from skillmind.runs.creation_request import TaskRunIntent
from skillmind.runs.domain import IdempotencyConflictError, derive_task_id
from skillmind.runs.repository import RunRepository
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation
from tests.runs.effect_authorization_harness import AuthorizationHarness
from tests.runs.execution_event_fakes import execution_event
from tests.runs.test_document_proposals import document_proposal
from tests.runs.test_execution_gates import execution_rows


def freeze_consent(run, *, enabled=True):
    """同じ actor/版/Run の作成要求を本番形式/hash で固定する。"""
    intent = TaskRunIntent(
        project_id=run.project_id,
        skill_version_id=UUID(run.task_snapshot_json["skill_version_id"]),
        task_key="review",
        actor_id=UUID(run.permission_snapshot_json["actor_id"]),
        input_json={},
        sources={},
        auto_approve=enabled,
    )
    run.task_snapshot_json.update(task_key=intent.task_key, creation_request=intent.to_json())
    run.task_id = derive_task_id(skill_version_id=intent.skill_version_id, task_key=intent.task_key)
    run.input_json, run.limits_snapshot_json = {}, {}
    run.selected_sources_json = getattr(run, "selected_sources_json", {}) or {}
    run.idempotency_key, run.request_hash = "start-consent", intent.fingerprint()
    return intent


def test_consent_is_part_of_identity_and_legacy_requests_stay_manual():
    """同じ key で後から権限を増減できず、v1 の hash は変わらない。"""
    manual = creation_intent()
    automatic = replace(manual, auto_approve=True)
    assert manual.to_json()["request_version"] == "v1"
    assert automatic.to_json()["request_version"] == "v2"
    assert TaskRunIntent.from_json(automatic.to_json()) == automatic
    for original, changed in ((manual, automatic), (automatic, manual)):
        run = stored_creation(creation_command(original))
        validate_creation_replay(run, original)
        with pytest.raises(IdempotencyConflictError):
            validate_creation_replay(run, changed)
    legacy = stored_creation(creation_command(manual, legacy=True))
    assert run_auto_approval_actor(legacy, "database.write/v1") is None


@pytest.mark.parametrize("capability", ["repository.write/v1", "issue.update/v1"])
def test_start_consent_does_not_enable_unregistered_automatic_providers(capability):
    """既存 Project policy とコード書込の承認境界を Run の checkbox で広げない。"""
    run = stored_creation(creation_command(replace(creation_intent(), auto_approve=True)))
    assert run_auto_approval_actor(run, capability) is None


@pytest.mark.parametrize("document", [False, True])
@pytest.mark.parametrize(
    "mutation", [None, "hash", "actor", "member", "project", "cancel", "version"]
)
async def test_automatic_approval_rechecks_frozen_consent_and_current_authority(
    monkeypatch,
    document,
    mutation,
):
    """PG/文書とも元同意と現在資格を各段階で再検証し、変更後に外部処理へ進めない。"""
    h = await document_proposal(monkeypatch) if document else AuthorizationHarness()
    freeze_consent(h.run)
    h.approval.source, h.approval.preauthorization_id = "RUN_START", None
    if mutation == "hash":
        h.run.request_hash = "0" * 64
    elif mutation == "actor":
        h.approval.actor_id = uuid4()
    elif mutation == "member":
        h.member.status = "DISABLED"
    elif mutation == "project":
        h.project.status = "ARCHIVED"
    elif mutation == "cancel":
        h.repository.is_cancellation_requested.return_value = True
    elif mutation == "version":
        h.approval.proposal_version = 2
    call = h.repository.authorize_effect_step(
        h.claimed, provider_version=h.execution.provider_version
    )
    if mutation is None:
        await call
    else:
        with pytest.raises(
            (EffectLeaseValidationError, ProjectArchivedError, ProjectNotFoundError)
        ):
            await call


@pytest.mark.parametrize("automatic", [False, True])
async def test_proposal_creates_one_audited_effect_or_one_manual_interaction(
    monkeypatch, automatic
):
    """同じ有効提案が自動時は独立 Effect Outbox、手動時は OPEN interaction に分岐する。"""
    claimed, run, segment, attempt = execution_rows()
    run.row_version = 1
    run.permission_snapshot_json = {"actor_id": str(uuid4())}
    run.task_snapshot_json = {"skill_version_id": str(uuid4()), "skill_snapshots": []}
    freeze_consent(run, enabled=automatic)
    event = execution_event(claimed, AgentEventType.CHANGE_PROPOSED)
    draft = replace(
        parse_change_proposal_request(event.payload["change_proposal_request"]),
        capability_version="database.write/v1",
        operation="INSERT",
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)
    repository = RunRepository(session, execution_features=ExecutionFeatures(database_writes=True))
    binding = SimpleNamespace(
        id=uuid4(), integration_id=uuid4(), provider="postgres", resource_kind="database"
    )
    monkeypatch.setattr(
        repository, "_lock_claimed_execution", AsyncMock(return_value=(run, segment, attempt))
    )
    monkeypatch.setattr(repository, "_next_sequence", AsyncMock(return_value=1))
    monkeypatch.setattr(
        repository, "_validate_proposal_draft", AsyncMock(return_value=(None, binding, None, {}))
    )
    monkeypatch.setattr(repository, "_validate_checkpoint_refs", AsyncMock())
    monkeypatch.setattr(repository, "_validate_evidence_refs", AsyncMock())
    agent = AgentSession(id=uuid4(), sdk_session_id=UUID(event.agent_session_id), usage_json={})
    monkeypatch.setattr(repository, "_ensure_agent_session", AsyncMock(return_value=agent))
    definition = replace(
        resolve_effect_capability("database.write/v1"), requested_scope=lambda _: {}
    )
    monkeypatch.setattr(
        "skillmind.runs.repository_effects.resolve_effect_capability",
        lambda _: definition,
    )
    await repository.suspend_for_proposal(
        claimed, event=event, session_metadata=MagicMock(), draft=draft
    )
    rows = session.add_all.call_args.args[0]
    proposals = [row for row in rows if isinstance(row, ChangeProposal)]
    approvals = [row for row in rows if isinstance(row, ChangeApproval)]
    effects = [row for row in rows if isinstance(row, EffectExecution)]
    interactions = [row for row in rows if isinstance(row, UserInteraction)]
    assert len(proposals) == 1
    assert len(approvals) == len(effects) == int(automatic)
    assert len(interactions) == int(not automatic)
    if automatic:
        assert approvals[0].source == "RUN_START"
        assert str(approvals[0].actor_id) == run.permission_snapshot_json["actor_id"]
        assert approvals[0].proposal_checksum == proposals[0].checksum
        assert approvals[0].proposal_id == effects[0].proposal_id
        assert effects[0].approval_id == approvals[0].id
        assert any(getattr(row, "topic", None) == "effect.apply.requested/v1" for row in rows)
        assert session.flush.await_count == 3
    else:
        assert interactions[0].status == "OPEN"


async def test_start_choice_is_frozen_by_authorized_service_transaction():
    """HTTP から受けた同意を、現在 actor の作成 transaction と初期 dispatch に固定する。"""
    from tests.runs.creation_authorization_harness import CreationAuthorizationHarness

    h = CreationAuthorizationHarness("new")
    result = await h.service.create_task_run(
        project_id=h.intent.project_id,
        resolved=h.resolved,
        input_json=h.input_json,
        sources=h.sources,
        actor_id=h.intent.actor_id,
        idempotency_key=h.key,
        authorization=h.access,
        trace_id="synthetic",
        auto_approve=True,
    )
    assert result is not None
    run = next(row for row in h.committed if getattr(row, "id", None) == result.run_id)
    assert run_auto_approval_actor(run, "database.write/v1") == h.intent.actor_id
    assert run.task_snapshot_json["creation_request"]["auto_approve"] is True
