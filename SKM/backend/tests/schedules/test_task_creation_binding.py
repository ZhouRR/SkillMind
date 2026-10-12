"""新規 Task gate が調度の原認領・重放・未知結果を越えて権限を作らないことを検証する。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skillmind.db.models import Run
from skillmind.effects.run_approval import run_auto_approval_actor
from skillmind.runs.creation_request import CREATION_REQUEST_FIELD
from skillmind.schedules.creation import ScheduleRunCreationParticipant
from skillmind.schedules.domain import (
    ScheduleClaimLostError,
    ScheduleOccurrenceConflictError,
    ScheduleOutcome,
)
from skillmind.schedules.service import ScheduleService
from skillmind.skills.domain import PublishedTaskNotFoundError
from tests.runs.creation_authorization_harness import row_values
from tests.runs.test_creation_authorization import CreationClock
from tests.schedules.fakes import NOW
from tests.schedules.task_creation_harness import ScheduledTaskCreationHarness


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
async def test_scheduled_run_checks_current_binding_only_for_new_insert(replay: bool) -> None:
    """元認領・資格・INSERT・共有 guard を実装のまま通し、旧 Run は現在の停用から分離する。"""

    db = ScheduledTaskCreationHarness(replay=replay)
    db.task_binding.binding = None
    before = row_values(db.occurrence)
    if replay:
        result = await db.call()
        assert result.run_id == db.winner.id and result.idempotent_replay
        assert db.occurrence.run_id == db.winner.id and db.occurrence.status == "SETTLED"
        assert "skill" not in db.events
    else:
        with pytest.raises(PublishedTaskNotFoundError):
            await db.call()
        assert row_values(db.occurrence) == before
        assert not any(isinstance(row, Run) for row in db.committed)
        assert db.rollbacks == 1 and not db.staged
        assert db.events.count("lock:Organization") == 2


@pytest.mark.asyncio
async def test_claim_expiry_during_unavailable_task_lookup_is_not_business_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有効化待機で原 lease の期限を跨いだ場合、Task 不在より claim 拒否を優先する。"""

    db = ScheduledTaskCreationHarness()
    CreationClock.current = NOW
    monkeypatch.setattr("skillmind.schedules.repository_occurrences.datetime", CreationClock)
    db.task_binding.binding = None

    def advance(event: str) -> None:
        """保存済み expiry を変えず、guard SELECT 後に現在時刻だけを進める。"""

        if event == "lock:ProjectSkillVersion":
            CreationClock.current = NOW + timedelta(seconds=61)

    db.on_step = advance
    with pytest.raises(ScheduleClaimLostError):
        await db.call()
    assert db.occurrence.status == "PENDING" and db.occurrence.run_id is None
    assert db.schedule.run_count == 0 and db.rollbacks == 1
    assert not any(isinstance(row, Run) for row in db.committed)


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_scheduled_creation_commit_unknown_is_not_settled_as_task_rejection(
    committed: bool,
) -> None:
    """共有 gate 後の commit 応答喪失を成功/前提拒否へ偽装せず、原認領照会に残す。"""

    db = ScheduledTaskCreationHarness()
    db.commit_unknown = committed
    with pytest.raises(ConnectionError):
        await db.call()
    assert db.occurrence.status == ("SETTLED" if committed else "PENDING")
    assert db.schedule.run_count == int(committed)
    assert sum(isinstance(row, Run) for row in db.committed) == int(committed)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "claim", "database", "cancel"])
async def test_schedule_trigger_classifies_only_determined_unavailable_task(failure: str) -> None:
    """実 RunService/共有 guard の拒否を trigger に返し、未知・取消・失認領は伝播させる。"""

    db = ScheduledTaskCreationHarness()
    if failure in {"unavailable", "claim"}:
        db.task_binding.binding = None
    errors: dict[str, type[BaseException]] = {
        "claim": ScheduleClaimLostError,
        "database": ConnectionError,
        "cancel": asyncio.CancelledError,
    }

    def fail_at_gate(event: str) -> None:
        """外部呼出なしで実 SELECT 待機のエラーだけを注入する。"""

        if event == "lock:ProjectSkillVersion" and failure in errors:
            raise errors[failure]("Synthetic unavailable boundary")

    db.on_step = fail_at_gate
    skills = MagicMock()
    skills.resolve_task_run = AsyncMock(return_value=db.resolved)
    service = ScheduleService(db.session_factory, skill_service=skills, run_service=db.service)
    with (
        patch.object(service, "_authorize_creator", AsyncMock(return_value=db.access.actor)),
        patch.object(service, "_overlapping_run", AsyncMock(return_value=None)),
    ):
        if failure == "unavailable":
            result = await service._create_scheduled_run(db.claim)
            assert result.outcome is ScheduleOutcome.FAILED_PRECONDITION and result.run_id is None
        else:
            with pytest.raises(errors[failure]):
                await service._create_scheduled_run(db.claim)
    assert db.occurrence.status == "PENDING" and db.schedule.run_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
async def test_scheduled_run_preserves_automatic_approval_in_original_request(replay: bool) -> None:
    """内部認領で原同意を検証し、Run 作成と再照会が同じ承認設定を使う。"""
    db = ScheduledTaskCreationHarness(replay=replay, auto_approve=True)
    result = await db.call()
    rows = [row for row in db.committed if isinstance(row, Run) and row.id == result.run_id]
    assert len(rows) == 1
    run = rows[0]
    original = run.task_snapshot_json[CREATION_REQUEST_FIELD]
    assert original["auto_approve"] is True
    assert original["auto_approve_git"] is True
    assert original["auto_approve_mcp"] is True
    for capability, provider in [
        ("http.write/v1", "http"),
        ("database.execute/v1", "postgres"),
        ("repository.write/v1", "git"),
        ("mcp.call/v1", "mcp"),
    ]:
        assert run_auto_approval_actor(run, capability, provider=provider) == db.user.id
    assert db.schedule.run_count == 1
    assert db.occurrence.status == "SETTLED" and db.occurrence.run_id == result.run_id


@pytest.mark.asyncio
async def test_schedule_claim_cannot_add_approval_to_another_original_request() -> None:
    """Worker の bool 改変だけで保存済み原 occurrence の同意を拡張しない。"""
    db = ScheduledTaskCreationHarness()
    with pytest.raises(ScheduleOccurrenceConflictError):
        await db.service.create_task_run(
            project_id=db.project.id,
            resolved=db.resolved,
            input_json=db.intent.input_json,
            sources=db.intent.sources,
            idempotency_key=db.key,
            trace_id=None,
            actor_id=db.user.id,
            authorization=ScheduleRunCreationParticipant(db.claim),
            auto_approve=True,
            auto_approve_git=True,
            auto_approve_mcp=True,
        )
    assert not any(isinstance(row, Run) for row in db.committed)
    assert db.occurrence.run_id is None
