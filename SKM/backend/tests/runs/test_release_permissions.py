"""新規 Run の配備上限と、既存 snapshot を書換えない重放を検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from skillmind.db.models import Run
from skillmind.runs.domain import TaskSourceSelectionError
from skillmind.runs.service import RunService
from tests.runs.creation_authorization_harness import CreationAuthorizationHarness, row_values


@pytest.mark.asyncio
async def test_readonly_creation_does_not_grant_subagent_or_proposal():
    """主読取とユーザー質問は保持し、後置能力を新規 permission snapshot に含めない。"""
    db = CreationAuthorizationHarness()
    db.service = RunService(db.session_factory, deferred_features_enabled=False)
    created = await db.call()
    assert created is not None and not created.idempotent_replay
    run = next(row for row in db.committed if isinstance(row, Run))
    assert set(run.permission_snapshot_json["allowed_capabilities"]) == {
        "repository.read/v1",
        "interaction.request/v1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability", ["issue.update/v1", "subagent.dispatch/v1", "change.propose/v1"]
)
async def test_required_deferred_execution_is_rejected_before_run_insert(capability):
    """Task の必須処理を黙って削った実行として作らず、保存前に拒否する。"""
    db = CreationAuthorizationHarness()
    db.service = RunService(db.session_factory, deferred_features_enabled=False)
    snapshot = deepcopy(db.resolved.skill_snapshot)
    snapshot["manifest"]["tools"] = [{"capability": capability, "required": True}]
    db.resolved = replace(db.resolved, allowed_capabilities=(capability,), skill_snapshot=snapshot)
    with pytest.raises(TaskSourceSelectionError, match="disabled execution features"):
        await db.call()
    assert db.committed == []
    db.session.add_all.assert_not_called()


@pytest.mark.asyncio
async def test_switch_to_readonly_does_not_rewrite_original_replay():
    """旧実行の参照は返せるが、snapshot を縮小して新しい実行許可にはしない。"""
    db = CreationAuthorizationHarness("first-replay")
    db.service = RunService(db.session_factory, deferred_features_enabled=False)
    original = row_values(db.winner)
    result = await db.call()
    assert result is not None and result.idempotent_replay
    assert row_values(db.winner) == original


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_scheduled_creation_has_an_independent_deployment_limit(enabled):
    """調度の認領から作成まで単独開関を通し、後置 tool の権限は増やさない。"""
    from tests.schedules.task_creation_harness import ScheduledTaskCreationHarness

    db = ScheduledTaskCreationHarness()
    db.service = RunService(db.session_factory, deferred_features_enabled=False,
                            scheduling_enabled=enabled)
    if not enabled:
        with pytest.raises(TaskSourceSelectionError, match='Scheduled execution is disabled'):
            await db.call()
        assert not any(isinstance(row, Run) for row in db.committed)
    else:
        await db.call()
        run = next(row for row in db.committed if isinstance(row, Run))
        assert db.occurrence.run_id == run.id
        assert db.occurrence.status == 'SETTLED'
        assert set(run.permission_snapshot_json['allowed_capabilities']) == {
            'repository.read/v1', 'interaction.request/v1',
        }
