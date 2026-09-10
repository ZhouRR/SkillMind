"""RunRepository の idempotent create と transaction 集約を検証する。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.agent.domain import AgentEvent, AgentEventType
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    AgentSession,
    Evidence,
    OutboxMessage,
    Project,
    ProjectSkillVersion,
    Run,
    RunAttempt,
    RunEvent,
    RunResult,
    RunSegment,
    RunSkillSnapshot,
    RuntimeManifest,
    SkillVersion,
    ToolCall,
    UserInteraction,
)
from skillmind.runs.domain import (
    AgentSessionMetadata,
    ClaimedRun,
    CreateRunCommand,
    IdempotencyConflictError,
    InteractionExpiredError,
    LeaseValidationError,
    RunAttemptStatus,
    RunCancellationState,
    RunResultRecord,
    RunSegmentStatus,
    RunSegmentTrigger,
    RunStatus,
    SessionContinuationMode,
    UserInteractionStatus,
    UserInteractionType,
    request_hash,
)
from skillmind.runs.interaction import InteractionRequestDraft
from skillmind.runs.repository import RunRepository
from tests.runs.task_binding_fakes import TaskBindingRows


def create_command() -> CreateRunCommand:
    """Repository test 用の固定 command を生成する。"""

    task_id = uuid4()
    return CreateRunCommand(
        project_id=uuid4(),
        task_id=task_id,
        idempotency_key="request-001",
        input_json={
            "ticket_id": "ISSUE-1234",
            "issue_source": "redmine",
            "repository_source": "svn",
        },
        task_snapshot_json={"task_id": str(task_id)},
        permission_snapshot_json={"mode": "auto_read_only"},
        selected_sources_json={"issue_source": "redmine"},
        limits_snapshot_json={"max_turns": 20},
        trace_id="trace-1",
    )


def mock_session(*, inserted: bool, existing: Run | None = None) -> MagicMock:
    """PostgreSQL statement の結果だけを置き換えた AsyncSession mock を返す。"""

    session = MagicMock(spec=AsyncSession)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = uuid4() if inserted else None
    session.execute = AsyncMock(return_value=execute_result)
    scalar_result = MagicMock()
    scalar_result.one_or_none.return_value = existing
    session.scalars = AsyncMock(return_value=scalar_result)
    return session


@pytest.mark.asyncio
async def test_create_adds_initial_event_and_dispatch_outbox() -> None:
    """新規 Run が初期 event と dispatch intent を同じ session に追加することを確認する。"""

    session = mock_session(inserted=True)
    created = await RunRepository(session).create_idempotent(create_command())

    assert created.idempotent_replay is False
    added = session.add_all.call_args.args[0]
    assert len(added) == 3
    assert isinstance(added[0], RunSegment)
    assert isinstance(added[1], RunEvent)
    assert isinstance(added[2], OutboxMessage)
    assert added[2].topic == "run.dispatch.requested/v1"


@pytest.mark.asyncio
async def test_create_freezes_explicit_published_skill_version() -> None:
    """Run 作成が published Version/checksum を検証し RunSkillSnapshot を同時追加する。"""

    command = create_command()
    version_id = uuid4()
    manifest_payload = {"manifest_version": "skillmind/v1alpha1"}
    checksum = f"sha256:{sha256_hex(canonical_json(manifest_payload))}"
    command = CreateRunCommand(
        project_id=command.project_id,
        task_id=command.task_id,
        idempotency_key=command.idempotency_key,
        input_json=command.input_json,
        task_snapshot_json=command.task_snapshot_json,
        permission_snapshot_json=command.permission_snapshot_json,
        selected_sources_json=command.selected_sources_json,
        limits_snapshot_json=command.limits_snapshot_json,
        trace_id=command.trace_id,
        skill_snapshots_json=(
            {
                "skill_version_id": str(version_id),
                "sort_order": 0,
                "manifest_checksum": checksum,
                "manifest": manifest_payload,
                "config_snapshot": {},
            },
        ),
    )
    version = MagicMock(spec=SkillVersion)
    version.id = version_id
    version.status = "PUBLISHED"
    manifest = MagicMock(spec=RuntimeManifest)
    manifest.checksum = checksum
    manifest.manifest_json = manifest_payload
    session = mock_session(inserted=True)
    session.get = AsyncMock(return_value=version)
    project = Project(id=command.project_id, organization_id=uuid4())
    binding = TaskBindingRows(project, version)

    async def scalar(statement: Select[Any]) -> object:
        """元 Project の組織と実共有 guard の二つの SQL だけを評価する。"""

        if statement.column_descriptions[0]["expr"] is Project.organization_id:
            assert statement.compile().params == {"id_1": command.project_id}
            return project.organization_id
        assert statement.column_descriptions[0]["entity"] in (SkillVersion, ProjectSkillVersion)
        return binding.scalar(statement)

    session.scalar = AsyncMock(side_effect=scalar)
    manifest_result = MagicMock()
    manifest_result.one_or_none.return_value = manifest
    session.scalars = AsyncMock(return_value=manifest_result)

    await RunRepository(session).create_idempotent(command)

    added = session.add_all.call_args.args[0]
    snapshots = [item for item in added if isinstance(item, RunSkillSnapshot)]
    assert len(snapshots) == 1
    assert snapshots[0].skill_version_id == version_id
    assert snapshots[0].manifest_checksum == checksum


@pytest.mark.asyncio
async def test_list_events_preserves_agent_session_identity() -> None:
    """SSE 詳細表示が永続化済み AgentSession を null に置き換えないことを確認する。"""

    session = MagicMock(spec=AsyncSession)
    session_id = uuid4()
    event = RunEvent(
        id=uuid4(),
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=session_id,
        sequence=4,
        event_type="SESSION_STARTED",
        payload_json={"model": "test"},
        occurred_at=datetime.now(UTC),
        trace_id="trace-1",
        summary="session started",
    )
    result = MagicMock()
    result.all.return_value = [event]
    session.scalars = AsyncMock(return_value=result)

    stored = await RunRepository(session).list_events(event.run_id, after=3)

    assert stored[0].agent_session_id == session_id


@pytest.mark.asyncio
async def test_queued_run_cancellation_writes_terminal_snapshot() -> None:
    """未実行 Run の取消が直ちに CANCELLED と最後の snapshot を同時保存する。"""

    run = create_queued_run()
    session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.one_or_none.return_value = run
    session.scalars = AsyncMock(return_value=result)
    session.scalar = AsyncMock(return_value=2)

    cancelled = await RunRepository(session).request_cancellation(run.id, trace_id="trace-1")

    assert cancelled.cancellation is RunCancellationState.CANCELLED
    assert cancelled.run.status is RunStatus.CANCELLED
    added = session.add_all.call_args.args[0]
    assert added[0].event_type == "RUN_SNAPSHOT"
    assert added[0].payload_json["status"] == RunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_running_run_cancellation_persists_idempotent_intent() -> None:
    """実行中 Run は row version を変えず、Worker が復旧可能な取消 intent を残す。"""

    run = create_queued_run()
    run.status = RunStatus.RUNNING.value
    run.row_version = 3
    session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.one_or_none.return_value = run
    session.scalars = AsyncMock(return_value=result)
    attempt_id = uuid4()
    session.scalar = AsyncMock(side_effect=[None, attempt_id, 4])

    cancelled = await RunRepository(session).request_cancellation(run.id, trace_id="trace-1")

    assert cancelled.cancellation is RunCancellationState.REQUESTED
    assert cancelled.run.status is RunStatus.RUNNING
    assert cancelled.run.row_version == 3
    added = session.add_all.call_args.args[0]
    assert added[0].event_type == "RUN_CANCEL_REQUESTED"
    assert added[0].run_attempt_id == attempt_id
    assert added[0].sequence == 4


@pytest.mark.asyncio
async def test_get_detail_projects_result_tool_calls_and_evidence() -> None:
    """Project ownership 済み detail が raw Tool result を公開 projection に含めない。"""

    run = create_queued_run()
    now = datetime(2026, 7, 2, 13, 0, tzinfo=UTC)
    result = RunResult(
        id=uuid4(),
        run_id=run.id,
        agent_session_id=uuid4(),
        output_schema="synthetic/issue-review/v1/output.schema.json",
        result_kind="STRUCTURED_OUTPUT",
        data_json={"issue": {"id": "fixture-001"}},
        evidence_refs_json=["ev_fixture_001"],
        artifact_refs_json=[],
        change_proposal_refs_json=[],
        optional_schema_identity_json={
            "schema_ref": "synthetic/issue-review/v1/output.schema.json"
        },
        summary="completed",
        confidence=0.8,
        needs_review=False,
        usage_json={},
        cost_json={},
        validation_json={"schema_valid": True},
        created_at=now,
    )
    tool_call = ToolCall(
        id=uuid4(),
        run_id=run.id,
        run_attempt_id=uuid4(),
        agent_session_id=uuid4(),
        sdk_tool_use_id="tool-1",
        request_fingerprint="1" * 64,
        tool_name="issue_read_v1",
        capability_version="issue.read/v1",
        provider="csv-fixture",
        integration_id=None,
        arguments_summary={"ticket_id": "fixture-001"},
        status="SUCCEEDED",
        duration_ms=10,
        result_json={"private": "not exposed"},
        error_json=None,
        created_at=now,
        updated_at=now,
    )
    evidence = Evidence(
        id=uuid4(),
        evidence_ref="ev_fixture_001",
        run_id=run.id,
        tool_call_id=tool_call.id,
        evidence_type="ticket-row",
        source_uri="fixture://synthetic/tickets.csv",
        source_locator={"row": 1},
        content_hash="sha256:" + ("1" * 64),
        snapshot_uri=None,
        excerpt="sanitized",
        metadata_json={},
        created_at=now,
    )
    run_skill = RunSkillSnapshot(
        id=uuid4(),
        run_id=run.id,
        skill_version_id=uuid4(),
        sort_order=0,
        config_snapshot_json={},
        manifest_checksum="sha256:" + ("3" * 64),
        created_at=now,
    )
    session = MagicMock(spec=AsyncSession)

    def scalar_result(
        *, one: object | None = None, all_items: list[object] | None = None
    ) -> MagicMock:
        """SQLAlchemy ScalarResult の one/all 応答を構築する。"""

        value = MagicMock()
        value.one_or_none.return_value = one
        value.all.return_value = all_items or []
        return value

    session.scalars = AsyncMock(
        side_effect=[
            scalar_result(one=run),
            scalar_result(one=result),
            scalar_result(all_items=[tool_call]),
            scalar_result(all_items=[evidence]),
            scalar_result(all_items=[run_skill]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
            scalar_result(all_items=[]),
        ]
    )

    detail = await RunRepository(session).get_detail(project_id=run.project_id, run_id=run.id)

    assert detail.result is not None
    assert detail.result.summary == "completed"
    assert detail.tool_calls[0].arguments_summary == {"ticket_id": "fixture-001"}
    assert not hasattr(detail.tool_calls[0], "result_json")
    assert detail.evidence[0].evidence_ref == "ev_fixture_001"
    assert detail.skill_snapshots[0].skill_version_id == run_skill.skill_version_id


@pytest.mark.asyncio
async def test_list_history_uses_limit_plus_one_for_pagination() -> None:
    """Project Run history が新しい順の page と has_more を一回の query で返す。"""

    now = datetime(2026, 7, 2, 13, 0, tzinfo=UTC)
    runs = [create_queued_run() for _ in range(3)]
    project_id = uuid4()
    for index, run in enumerate(runs):
        run.project_id = project_id
        run.created_at = now + timedelta(minutes=index)
    result = RunResult(
        id=uuid4(),
        run_id=runs[1].id,
        agent_session_id=uuid4(),
        output_schema="synthetic/issue-review/v1/output.schema.json",
        result_kind="STRUCTURED_OUTPUT",
        data_json={},
        evidence_refs_json=[],
        artifact_refs_json=[],
        optional_schema_identity_json={
            "schema_ref": "synthetic/issue-review/v1/output.schema.json"
        },
        summary="completed",
        confidence=0.9,
        needs_review=False,
        usage_json={},
        cost_json={},
        validation_json={"schema_valid": True},
        created_at=now,
    )
    execute_result = MagicMock()
    execute_result.all.return_value = [
        (runs[2], None),
        (runs[1], result),
        (runs[0], None),
    ]
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=execute_result)

    page = await RunRepository(session).list_history(
        project_id=project_id,
        limit=2,
        offset=0,
    )

    assert len(page.items) == 2
    assert page.has_more is True
    assert page.items[1].result_summary == "completed"


@pytest.mark.asyncio
async def test_interaction_response_completes_segment_and_dispatches_next() -> None:
    """User response が retry ではなく次 Segment と単調 event 列を同時追加する。"""

    run = create_queued_run()
    run.status = RunStatus.WAITING_FOR_INPUT.value
    run.row_version = 4
    segment = create_segment(run, status=RunSegmentStatus.WAITING)
    now = datetime.now(UTC)
    interaction = UserInteraction(
        id=uuid4(),
        run_id=run.id,
        run_segment_id=segment.id,
        agent_session_id=uuid4(),
        interaction_type=UserInteractionType.REVIEW.value,
        prompt_json={
            "prompt": "Review the finding.",
            "rationale": "Project context is required.",
            "impact": "The recommendation may change.",
            "allow_multiple": False,
        },
        options_json=[],
        required=True,
        expires_at=now + timedelta(hours=1),
        status=UserInteractionStatus.OPEN.value,
        version=1,
        continuation_mode=SessionContinuationMode.FORK.value,
        checkpoint_json={
            "summary": "Analysis is ready for review.",
            "confirmed_facts": [],
            "evidence_refs": [],
            "artifact_refs": [],
            "change_proposal_refs": [],
        },
        checkpoint_checksum="sha256:" + ("7" * 64),
        created_at=now,
        updated_at=now,
    )
    session = MagicMock(spec=AsyncSession)

    def one_or_none(value: object | None) -> MagicMock:
        """一件 query の ScalarResult mock を返す。"""

        result = MagicMock()
        result.one_or_none.return_value = value
        return result

    locked_interaction = MagicMock()
    locked_interaction.one_or_none.return_value = interaction
    session.scalars = AsyncMock(
        side_effect=[
            one_or_none(interaction),
            one_or_none(run),
            one_or_none(segment),
            locked_interaction,
            one_or_none(None),
        ]
    )
    session.scalar = AsyncMock(return_value=20)

    responded = await RunRepository(session).respond_to_interaction(
        project_id=run.project_id,
        run_id=run.id,
        interaction_id=interaction.id,
        actor_id=uuid4(),
        interaction_version=1,
        response_json={"text": "Keep the current finding."},
        idempotency_key="interaction-response-001",
        trace_id="trace-interaction-1",
    )

    assert responded.segment_no == 2
    assert responded.continuation_mode is SessionContinuationMode.FORK
    assert responded.run.status is RunStatus.QUEUED
    assert interaction.status == UserInteractionStatus.RESPONDED.value
    assert segment.status == RunSegmentStatus.COMPLETED.value
    added = session.add_all.call_args.args[0]
    next_segments = [item for item in added if isinstance(item, RunSegment)]
    assert len(next_segments) == 1
    assert next_segments[0].segment_no == 2
    assert next_segments[0].parent_agent_session_id == interaction.agent_session_id
    events = [item for item in added if isinstance(item, RunEvent)]
    assert [(item.sequence, item.event_type) for item in events] == [
        (20, AgentEventType.SEGMENT_COMPLETED.value),
        (21, AgentEventType.INTERACTION_RESPONDED.value),
        (22, "RUN_SNAPSHOT"),
    ]
    dispatches = [
        item
        for item in added
        if isinstance(item, OutboxMessage) and item.topic == "run.dispatch.requested/v1"
    ]
    assert len(dispatches) == 1


@pytest.mark.asyncio
async def test_expired_interaction_creates_timeout_segment_without_default_response() -> None:
    """期限切れ回答を保存せず、明示 timeout と欠落情報を次 Segment へ渡す。"""

    run = create_queued_run()
    run.status = RunStatus.WAITING_FOR_INPUT.value
    run.row_version = 7
    segment = create_segment(run, status=RunSegmentStatus.WAITING)
    now = datetime.now(UTC)
    interaction = UserInteraction(
        id=uuid4(),
        run_id=run.id,
        run_segment_id=segment.id,
        agent_session_id=uuid4(),
        interaction_type=UserInteractionType.REVIEW.value,
        prompt_json={
            "prompt": "Review the finding.",
            "rationale": "Project context is required.",
            "impact": "The recommendation may change.",
            "allow_multiple": False,
        },
        options_json=[],
        required=True,
        expires_at=now - timedelta(seconds=1),
        status=UserInteractionStatus.OPEN.value,
        version=2,
        continuation_mode=SessionContinuationMode.FORK.value,
        checkpoint_json={
            "summary": "Analysis is ready for review.",
            "confirmed_facts": [],
            "evidence_refs": [],
            "artifact_refs": [],
            "change_proposal_refs": [],
        },
        checkpoint_checksum="sha256:" + ("6" * 64),
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    session = MagicMock(spec=AsyncSession)

    def one_or_none(value: object | None) -> MagicMock:
        """一件 query の ScalarResult mock を返す。"""

        result = MagicMock()
        result.one_or_none.return_value = value
        return result

    locked_interaction = MagicMock()
    locked_interaction.one_or_none.return_value = interaction
    session.scalars = AsyncMock(
        side_effect=[
            one_or_none(interaction),
            one_or_none(run),
            one_or_none(segment),
            locked_interaction,
            one_or_none(None),
        ]
    )
    session.scalar = AsyncMock(return_value=30)

    with pytest.raises(InteractionExpiredError):
        await RunRepository(session).respond_to_interaction(
            project_id=run.project_id,
            run_id=run.id,
            interaction_id=interaction.id,
            actor_id=uuid4(),
            interaction_version=2,
            response_json={"text": "Late response"},
            idempotency_key="interaction-response-late-001",
            trace_id="trace-interaction-timeout",
        )

    assert run.status == RunStatus.QUEUED.value
    assert run.row_version == 8
    assert interaction.status == UserInteractionStatus.EXPIRED.value
    assert interaction.version == 3
    assert segment.status == RunSegmentStatus.COMPLETED.value
    added = session.add_all.call_args.args[0]
    next_segment = next(item for item in added if isinstance(item, RunSegment))
    assert next_segment.trigger_type == RunSegmentTrigger.INTERACTION_TIMEOUT.value
    assert next_segment.parent_agent_session_id == interaction.agent_session_id
    assert "no default response was assumed" in next_segment.checkpoint_json["confirmed_facts"][-1]
    events = [item for item in added if isinstance(item, RunEvent)]
    assert [(item.sequence, item.event_type) for item in events] == [
        (30, AgentEventType.SEGMENT_COMPLETED.value),
        (31, AgentEventType.INTERACTION_EXPIRED.value),
        (32, "RUN_SNAPSHOT"),
    ]
    assert not any(item.__class__.__name__ == "InteractionResponse" for item in added)


@pytest.mark.asyncio
async def test_same_idempotent_request_returns_existing_run() -> None:
    """同一 fingerprint の競合 INSERT が既存 Run replay になることを確認する。"""

    command = create_command()
    existing = Run(
        id=uuid4(),
        project_id=command.project_id,
        task_id=command.task_id,
        trigger_type="immediate",
        idempotency_key=command.idempotency_key,
        request_hash=request_hash(command),
        status="QUEUED",
        input_json=command.input_json,
        task_snapshot_json=command.task_snapshot_json,
        permission_snapshot_json=command.permission_snapshot_json,
        selected_sources_json=command.selected_sources_json,
        limits_snapshot_json=command.limits_snapshot_json,
        row_version=1,
        created_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
    )
    session = mock_session(inserted=False, existing=existing)

    replay = await RunRepository(session).create_idempotent(command)

    assert replay.run_id == existing.id
    assert replay.idempotent_replay is True
    session.add_all.assert_not_called()


@pytest.mark.asyncio
async def test_changed_request_rejects_idempotency_reuse() -> None:
    """既存 Run と fingerprint が異なる request を conflict として拒否する。"""

    command = create_command()
    existing = Run(
        id=uuid4(),
        project_id=command.project_id,
        task_id=command.task_id,
        trigger_type="immediate",
        idempotency_key=command.idempotency_key,
        request_hash="0" * 64,
        status="QUEUED",
        input_json={},
        task_snapshot_json={},
        permission_snapshot_json={},
        selected_sources_json={},
        limits_snapshot_json={},
        row_version=1,
        created_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
    )
    session = mock_session(inserted=False, existing=existing)

    with pytest.raises(IdempotencyConflictError):
        await RunRepository(session).create_idempotent(command)


def create_queued_run() -> Run:
    """Lease test 用の QUEUED Run model を生成する。"""

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    actor_id = uuid4()
    return Run(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        trigger_type="immediate",
        idempotency_key="request-001",
        request_hash="0" * 64,
        status=RunStatus.QUEUED.value,
        input_json={"ticket_id": "ISSUE-1234"},
        task_snapshot_json={},
        permission_snapshot_json={"actor_id": str(actor_id)},
        selected_sources_json={},
        limits_snapshot_json={},
        row_version=1,
        created_at=now,
        updated_at=now,
    )


def create_segment(run: Run, *, status: RunSegmentStatus = RunSegmentStatus.CREATED) -> RunSegment:
    """Release G claim/recovery test 用の明示 Segment 1 を生成する。"""

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    return RunSegment(
        id=uuid4(),
        run_id=run.id,
        segment_no=1,
        trigger_type=RunSegmentTrigger.INITIAL.value,
        trigger_ref=None,
        status=status.value,
        objective_json={"source": "RUN_OBJECTIVE"},
        checkpoint_json={},
        continuation_mode=SessionContinuationMode.INITIAL.value,
        parent_agent_session_id=None,
        instruction_snapshot_id=None,
        started_at=now if status is RunSegmentStatus.RUNNING else None,
        finished_at=None,
        created_at=now,
        updated_at=now,
    )


def create_claimed_running() -> tuple[ClaimedRun, Run, RunAttempt]:
    """Execution transaction test 用の claim、Run、Attempt を生成する。"""

    run = create_queued_run()
    run.status = RunStatus.RUNNING.value
    run.row_version = 3
    token = "lease-token"
    attempt = RunAttempt(
        id=uuid4(),
        run_id=run.id,
        attempt_no=1,
        reason="INITIAL",
        status=RunAttemptStatus.RUNNING.value,
        worker_id="worker-1",
        lease_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        lease_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        heartbeat_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert attempt.lease_expires_at is not None
    claimed = ClaimedRun(
        run_id=run.id,
        run_attempt_id=attempt.id,
        project_id=run.project_id,
        actor_id=uuid4(),
        attempt_no=1,
        lease_token=token,
        lease_expires_at=attempt.lease_expires_at,
        row_version=2,
        input_json=run.input_json,
        task_snapshot_json=run.task_snapshot_json,
        permission_snapshot_json=run.permission_snapshot_json,
        selected_sources_json=run.selected_sources_json,
        limits_snapshot_json=run.limits_snapshot_json,
    )
    return claimed, run, attempt


@pytest.mark.asyncio
async def test_interaction_suspension_releases_lease_and_persists_checkpoint() -> None:
    """Agent の質問を保存して待機へ移る際、Worker lease と active Session を残さない。"""

    claimed, run, attempt = create_claimed_running()
    segment = create_segment(run, status=RunSegmentStatus.RUNNING)
    attempt.run_segment_id = segment.id
    claimed = replace(
        claimed,
        run_segment_id=segment.id,
        segment_no=segment.segment_no,
    )
    now = datetime.now(UTC)
    sdk_session_id = uuid4()
    event = AgentEvent(
        run_id=run.id,
        run_attempt_id=attempt.id,
        agent_session_id=str(sdk_session_id),
        sequence=10,
        occurred_at=now,
        event_type=AgentEventType.INTERACTION_REQUESTED,
        payload={"interaction_request": {}},
    )
    metadata = AgentSessionMetadata(
        cwd="/workspace/run",
        engine="claude-agent-sdk",
        sdk_version="test-sdk",
        cli_version="test-cli",
        model="claude-test",
    )
    request = InteractionRequestDraft(
        interaction_type=UserInteractionType.REVIEW,
        prompt={
            "prompt": "Review the finding.",
            "rationale": "Project context is required.",
            "impact": "The recommendation may change.",
            "allow_multiple": False,
        },
        options=(),
        required=True,
        expires_at=now + timedelta(hours=1),
        continuation_mode=SessionContinuationMode.FORK,
        checkpoint={
            "summary": "Analysis is ready for review.",
            "confirmed_facts": [],
            "evidence_refs": [],
            "artifact_refs": [],
            "change_proposal_refs": [],
        },
        checkpoint_checksum="sha256:" + ("8" * 64),
    )
    session = MagicMock(spec=AsyncSession)

    def scalar_result(value: object | None) -> MagicMock:
        """Lock query 用の一件 ScalarResult mock を返す。"""

        result = MagicMock()
        result.one_or_none.return_value = value
        return result

    session.scalars = AsyncMock(
        side_effect=[
            scalar_result(run),
            scalar_result(segment),
            scalar_result(attempt),
            scalar_result(None),
        ]
    )
    session.scalar = AsyncMock(side_effect=[None, 10, None])

    interaction_id = await RunRepository(session).suspend_for_interaction(
        claimed,
        event=event,
        session_metadata=metadata,
        request=request,
    )

    assert interaction_id
    assert run.status == RunStatus.WAITING_FOR_INPUT.value
    assert segment.status == RunSegmentStatus.WAITING.value
    assert attempt.status == RunAttemptStatus.DEFERRED.value
    assert attempt.lease_token_hash is None
    assert attempt.lease_expires_at is None
    agent_session = session.add.call_args.args[0]
    assert isinstance(agent_session, AgentSession)
    assert agent_session.status == "IDLE"
    added = session.add_all.call_args.args[0]
    interactions = [item for item in added if isinstance(item, UserInteraction)]
    assert len(interactions) == 1
    assert interactions[0].checkpoint_checksum == request.checkpoint_checksum
    events = [item for item in added if isinstance(item, RunEvent)]
    assert [(item.sequence, item.event_type) for item in events] == [
        (10, AgentEventType.INTERACTION_REQUESTED.value),
        (11, AgentEventType.CHECKPOINT_CREATED.value),
        (12, "RUN_SNAPSHOT"),
    ]


@pytest.mark.asyncio
async def test_claim_creates_attempt_event_and_outbox_atomically() -> None:
    """QUEUED Run の claim が Attempt、Event、通知を同じ session に追加する。"""

    run = create_queued_run()
    segment = create_segment(run)
    session = MagicMock(spec=AsyncSession)
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    segment_result = MagicMock()
    segment_result.one_or_none.return_value = segment
    session.scalars = AsyncMock(side_effect=[run_result, segment_result])
    session.scalar = AsyncMock(side_effect=[None, 1, 2])
    lease_token = "lease-token"

    claimed = await RunRepository(session).claim_for_execution(
        run.id,
        worker_id="worker-1",
        lease_token=lease_token,
        lease_token_hash=hashlib.sha256(lease_token.encode()).hexdigest(),
        lease_expires_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
        max_attempts=3,
    )

    assert claimed is not None
    assert claimed.attempt_no == 1
    assert claimed.row_version == 2
    assert run.status == RunStatus.PREPARING.value
    added = session.add_all.call_args.args[0]
    attempts = [item for item in added if isinstance(item, RunAttempt)]
    assert len(attempts) == 1
    assert attempts[0].run_segment_id == segment.id
    assert attempts[0].lease_token_hash != lease_token
    assert sum(isinstance(item, RunEvent) for item in added) == 2
    assert sum(isinstance(item, OutboxMessage) for item in added) == 2


@pytest.mark.asyncio
async def test_duplicate_claim_is_idempotent_noop() -> None:
    """既に PREPARING の Run を重複 Queue job が再 claim しないことを確認する。"""

    run = create_queued_run()
    run.status = RunStatus.PREPARING.value
    session = MagicMock(spec=AsyncSession)
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    session.scalars = AsyncMock(return_value=run_result)

    claimed = await RunRepository(session).claim_for_execution(
        run.id,
        worker_id="worker-2",
        lease_token="new-token",
        lease_token_hash="1" * 64,
        lease_expires_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
        max_attempts=3,
    )

    assert claimed is None
    session.add_all.assert_not_called()


@pytest.mark.asyncio
async def test_claim_beyond_max_attempts_finalizes_run_as_failed() -> None:
    """再試行上限を超えた Run が claim されず retry_exhausted で FAILED になることを確認する。"""

    run = create_queued_run()
    run.status = RunStatus.RETRY_PENDING.value
    segment = create_segment(run, status=RunSegmentStatus.RUNNING)
    session = MagicMock(spec=AsyncSession)
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    segment_result = MagicMock()
    segment_result.one_or_none.return_value = segment
    session.scalars = AsyncMock(side_effect=[run_result, segment_result])
    # Active attempt なし、次 attempt_no は 4、次 event sequence は 9 を返す。
    session.scalar = AsyncMock(side_effect=[None, 4, 9])

    claimed = await RunRepository(session).claim_for_execution(
        run.id,
        worker_id="worker-1",
        lease_token="lease-token",
        lease_token_hash="2" * 64,
        lease_expires_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
        max_attempts=3,
    )

    assert claimed is None
    assert run.status == RunStatus.FAILED.value
    assert run.error_json == {
        "code": "retry_exhausted",
        "retryable": False,
        "attempts": 3,
        "max_attempts": 3,
    }
    assert segment.status == RunSegmentStatus.FAILED.value
    added = session.add_all.call_args.args[0]
    assert isinstance(added[0], RunEvent)
    assert added[0].payload_json["status"] == RunStatus.FAILED.value
    assert isinstance(added[1], OutboxMessage)
    assert added[1].topic == "run.lifecycle.changed/v1"


@pytest.mark.asyncio
async def test_prepare_execution_moves_attempt_and_run_to_running() -> None:
    """Valid lease だけが PREPARING から RUNNING と次の event sequence を取得する。"""

    claimed, run, attempt = create_claimed_running()
    run.status = RunStatus.PREPARING.value
    run.row_version = claimed.row_version
    attempt.status = RunAttemptStatus.LEASED.value
    session = MagicMock(spec=AsyncSession)
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    attempt_result = MagicMock()
    attempt_result.one_or_none.return_value = attempt
    session.scalars = AsyncMock(side_effect=[run_result, attempt_result])
    session.scalar = AsyncMock(return_value=5)

    prepared = await RunRepository(session).prepare_execution(claimed)

    assert prepared.row_version == 3
    assert prepared.next_sequence == 6
    assert run.status == RunStatus.RUNNING.value
    assert attempt.status == RunAttemptStatus.RUNNING.value
    added = session.add_all.call_args.args[0]
    assert isinstance(added[0], RunEvent)
    assert added[0].sequence == 5
    assert isinstance(added[1], OutboxMessage)


@pytest.mark.asyncio
async def test_append_agent_event_creates_session_and_strips_structured_result() -> None:
    """Agent session を一度作り、大きな structured output は RunEvent に複製しない。"""

    claimed, run, attempt = create_claimed_running()
    session = MagicMock(spec=AsyncSession)
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    attempt_result = MagicMock()
    attempt_result.one_or_none.return_value = attempt
    agent_result = MagicMock()
    agent_result.one_or_none.return_value = None
    session.scalars = AsyncMock(side_effect=[run_result, attempt_result, agent_result])
    session.scalar = AsyncMock(side_effect=[None, 5])
    sdk_session_id = uuid4()
    event = AgentEvent(
        run_id=run.id,
        run_attempt_id=attempt.id,
        agent_session_id=str(sdk_session_id),
        sequence=5,
        occurred_at=datetime.now(UTC),
        event_type=AgentEventType.RESULT_COMPLETED,
        payload={"structured_output": {"secret": "large"}, "num_turns": 2},
    )

    await RunRepository(session).append_agent_event(
        claimed,
        event,
        session_metadata=AgentSessionMetadata(
            cwd="/runs/1/workspace",
            engine="claude-agent-sdk",
            sdk_version="0.2.110",
            cli_version="2.1.191",
            model="claude-test",
        ),
    )

    assert isinstance(session.add.call_args.args[0], AgentSession)
    added = session.add_all.call_args.args[0]
    assert added[0].payload_json == {"num_turns": 2}
    assert added[0].agent_session_id == sdk_session_id


@pytest.mark.asyncio
async def test_finalize_success_persists_result_and_terminal_snapshot_atomically() -> None:
    """Validated Result、Agent terminal event、Run snapshot、Outbox を一 transaction に集約する。"""

    claimed, run, attempt = create_claimed_running()
    session = MagicMock(spec=AsyncSession)
    run_query = MagicMock()
    run_query.one_or_none.return_value = run
    attempt_query = MagicMock()
    attempt_query.one_or_none.return_value = attempt
    agent_query = MagicMock()
    agent_query.one_or_none.return_value = None
    session.scalars = AsyncMock(side_effect=[run_query, attempt_query, agent_query])
    # 取消意図は無く、その確認後の次 sequence が 5 である。
    session.scalar = AsyncMock(side_effect=[None, 5])
    sdk_session_id = uuid4()
    event = AgentEvent(
        run_id=run.id,
        run_attempt_id=attempt.id,
        agent_session_id=str(sdk_session_id),
        sequence=5,
        occurred_at=datetime.now(UTC),
        event_type=AgentEventType.RESULT_COMPLETED,
        payload={"structured_output": {"issue": {}}, "num_turns": 2},
    )
    result = RunResultRecord(
        output_schema="synthetic/issue-review/v1/output.schema.json",
        result_kind="STRUCTURED_OUTPUT",
        data={"issue": {}},
        evidence_refs=(),
        artifact_refs=(),
        change_proposal_refs=(),
        optional_schema_identity={"schema_ref": "synthetic/issue-review/v1/output.schema.json"},
        summary="completed",
        confidence=0.8,
        needs_review=False,
        usage={"input_tokens": 10},
        cost={"total_cost_usd": 0.01},
        validation={"schema_valid": True},
    )

    await RunRepository(session).finalize_execution(
        claimed,
        target=RunStatus.SUCCEEDED,
        attempt_status=RunAttemptStatus.SUCCEEDED,
        event=event,
        session_metadata=AgentSessionMetadata(
            cwd="/runs/1/workspace",
            engine="claude-agent-sdk",
            sdk_version="0.2.110",
            cli_version="2.1.191",
            model="claude-test",
        ),
        result=result,
        error_json=None,
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert attempt.status == RunAttemptStatus.SUCCEEDED.value
    added_models = [call.args[0] for call in session.add.call_args_list]
    assert any(isinstance(model, AgentSession) for model in added_models)
    assert any(isinstance(model, RunResult) for model in added_models)
    aggregate = session.add_all.call_args.args[0]
    events = [item for item in aggregate if isinstance(item, RunEvent)]
    assert [item.event_type for item in events] == ["RESULT_COMPLETED", "RUN_SNAPSHOT"]
    assert events[0].payload_json == {"num_turns": 2}
    assert len([item for item in aggregate if isinstance(item, OutboxMessage)]) == 2


@pytest.mark.asyncio
async def test_heartbeat_rejects_wrong_lease_token() -> None:
    """Lease token hash が一致しない heartbeat を拒否する。"""

    attempt = RunAttempt(
        id=uuid4(),
        run_id=uuid4(),
        attempt_no=1,
        reason="INITIAL",
        status=RunAttemptStatus.LEASED.value,
        worker_id="worker-1",
        lease_token_hash="0" * 64,
        lease_expires_at=datetime(2026, 7, 1, 12, 1, tzinfo=UTC),
        heartbeat_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        created_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
    )
    session = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.one_or_none.return_value = attempt
    session.scalars = AsyncMock(return_value=result)

    with pytest.raises(LeaseValidationError, match="token"):
        await RunRepository(session).heartbeat_attempt(
            attempt.id,
            lease_token_hash="1" * 64,
            lease_expires_at=datetime(2026, 7, 1, 12, 2, tzinfo=UTC),
            now=datetime(2026, 7, 1, 12, 0, 30, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_expired_attempt_returns_run_to_retry_pending() -> None:
    """期限切れ lease が Attempt 履歴を残して Run を再 dispatch 可能にする。"""

    now = datetime(2026, 7, 1, 12, 2, tzinfo=UTC)
    run = create_queued_run()
    run.status = RunStatus.RUNNING.value
    run.row_version = 2
    attempt = RunAttempt(
        id=uuid4(),
        run_id=run.id,
        attempt_no=1,
        reason="INITIAL",
        status=RunAttemptStatus.RUNNING.value,
        worker_id="worker-1",
        lease_token_hash="0" * 64,
        lease_expires_at=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(seconds=61),
        started_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=1),
    )
    session = MagicMock(spec=AsyncSession)
    attempts_result = MagicMock()
    attempts_result.all.return_value = [attempt]
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    attempt_result = MagicMock()
    attempt_result.one_or_none.return_value = attempt
    empty_sessions = MagicMock()
    empty_sessions.all.return_value = []
    session.scalars = AsyncMock(
        side_effect=[attempts_result, run_result, attempt_result, empty_sessions]
    )
    session.scalar = AsyncMock(side_effect=[None, 3])

    recovered = await RunRepository(session).recover_expired_attempts(now=now, limit=20)

    assert recovered == 1
    assert attempt.status == RunAttemptStatus.LEASE_EXPIRED.value
    assert run.status == RunStatus.RETRY_PENDING.value
    added = session.add_all.call_args.args[0]
    assert len(added) == 3
    assert {item.topic for item in added if isinstance(item, OutboxMessage)} == {
        "run.lifecycle.changed/v1",
        "run.dispatch.requested/v1",
    }


@pytest.mark.asyncio
async def test_worker_loss_recovery_creates_second_attempt_without_snapshot_drift() -> None:
    """Worker 喪失後の再 claim が Attempt を追加し、Run snapshot を変更しない。"""

    now = datetime(2026, 7, 1, 12, 2, tzinfo=UTC)
    run = create_queued_run()
    run.status = RunStatus.RUNNING.value
    run.row_version = 2
    original_snapshots = (
        dict(run.input_json),
        dict(run.task_snapshot_json),
        dict(run.permission_snapshot_json),
    )
    first_attempt = RunAttempt(
        id=uuid4(),
        run_id=run.id,
        attempt_no=1,
        reason="INITIAL",
        status=RunAttemptStatus.RUNNING.value,
        worker_id="worker-1",
        lease_token_hash="0" * 64,
        lease_expires_at=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(seconds=61),
        started_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=1),
    )
    segment = create_segment(run, status=RunSegmentStatus.RUNNING)
    first_attempt.run_segment_id = segment.id
    recovery_session = MagicMock(spec=AsyncSession)
    candidates = MagicMock()
    candidates.all.return_value = [first_attempt]
    locked_run = MagicMock()
    locked_run.one_or_none.return_value = run
    locked_attempt = MagicMock()
    locked_attempt.one_or_none.return_value = first_attempt
    locked_segment = MagicMock()
    locked_segment.one_or_none.return_value = segment
    empty_sessions = MagicMock()
    empty_sessions.all.return_value = []
    recovery_session.scalars = AsyncMock(
        side_effect=[candidates, locked_run, locked_segment, locked_attempt, empty_sessions]
    )
    recovery_session.scalar = AsyncMock(side_effect=[None, 3])

    recovered = await RunRepository(recovery_session).recover_expired_attempts(
        now=now,
        limit=20,
    )

    claim_session = MagicMock(spec=AsyncSession)
    retry_run = MagicMock()
    retry_run.one_or_none.return_value = run
    retry_segment = MagicMock()
    retry_segment.one_or_none.return_value = segment
    claim_session.scalars = AsyncMock(side_effect=[retry_run, retry_segment])
    claim_session.scalar = AsyncMock(side_effect=[None, 2, 4])
    claimed = await RunRepository(claim_session).claim_for_execution(
        run.id,
        worker_id="worker-2",
        lease_token="second-token",
        lease_token_hash="1" * 64,
        lease_expires_at=now + timedelta(minutes=1),
        max_attempts=3,
    )

    assert recovered == 1
    assert first_attempt.status == RunAttemptStatus.LEASE_EXPIRED.value
    assert claimed is not None
    assert claimed.attempt_no == 2
    assert run.status == RunStatus.PREPARING.value
    assert (
        run.input_json,
        run.task_snapshot_json,
        run.permission_snapshot_json,
    ) == original_snapshots
    second_attempt = next(
        item for item in claim_session.add_all.call_args.args[0] if isinstance(item, RunAttempt)
    )
    assert second_attempt.reason == "RETRY"


@pytest.mark.asyncio
async def test_expired_attempt_with_cancel_intent_closes_run() -> None:
    """Worker 喪失後も durable cancel intent を再 dispatch せず CANCELLED へ回復する。"""

    now = datetime(2026, 7, 1, 12, 2, tzinfo=UTC)
    run = create_queued_run()
    run.status = RunStatus.RUNNING.value
    run.row_version = 2
    attempt = RunAttempt(
        id=uuid4(),
        run_id=run.id,
        attempt_no=1,
        reason="INITIAL",
        status=RunAttemptStatus.RUNNING.value,
        worker_id="worker-1",
        lease_token_hash="0" * 64,
        lease_expires_at=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(seconds=61),
        started_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=1),
    )
    session = MagicMock(spec=AsyncSession)
    candidates = MagicMock()
    candidates.all.return_value = [attempt]
    run_result = MagicMock()
    run_result.one_or_none.return_value = run
    attempt_result = MagicMock()
    attempt_result.one_or_none.return_value = attempt
    empty_sessions = MagicMock()
    empty_sessions.all.return_value = []
    session.scalars = AsyncMock(
        side_effect=[candidates, run_result, attempt_result, empty_sessions]
    )
    session.scalar = AsyncMock(side_effect=[uuid4(), 4])

    recovered = await RunRepository(session).recover_expired_attempts(now=now, limit=20)

    assert recovered == 1
    assert run.status == RunStatus.CANCELLED.value
    assert attempt.status == RunAttemptStatus.CANCELLED.value
    added = session.add_all.call_args.args[0]
    assert [item.topic for item in added if isinstance(item, OutboxMessage)] == [
        "run.lifecycle.changed/v1"
    ]
    assert added[0].event_type == "RUN_SNAPSHOT"
