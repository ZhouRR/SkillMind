"""鎖外 task 解析後の停用と、新規 INSERT/原重放の境界を実 SQL fake で検証する。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Select

from projectmind.auth.sessions import UnauthorizedSessionError
from projectmind.db.models import Organization, ProjectSkillVersion, SkillVersion
from projectmind.skills.domain import PublishedTaskNotFoundError
from tests.runs.creation_authorization_harness import CreationAuthorizationHarness, row_values
from tests.runs.test_creation_authorization import CreationClock


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
async def test_disable_committed_after_resolution_prevents_only_new_run(replay: bool) -> None:
    """先に解析した caller が組織 gate へ入る前の停用を見落とさない。実 PG lock は別証拠。"""

    db = CreationAuthorizationHarness("first-replay" if replay else "new")
    original = deepcopy(row_values(db.winner))
    resolved = db.resolved
    reached_gate, disabled = asyncio.Event(), asyncio.Event()
    original_scalar = db.scalar

    async def wait_at_organization(statement: Select[Any]) -> Any:
        """解析済み要求を gate 手前に止め、他 writer の commit 順序だけを明示する。"""

        if statement.column_descriptions[0]["entity"] is Organization:
            reached_gate.set()
            await disabled.wait()
        return await original_scalar(statement)

    db.session.scalar.side_effect = wait_at_organization
    task = asyncio.create_task(db.call())
    try:
        await asyncio.wait_for(reached_gate.wait(), timeout=2)
        assert db.task_binding.binding is not None
        db.task_binding.binding.disabled_at = datetime.now(UTC)
        disabled.set()
        if replay:
            receipt = await asyncio.wait_for(task, timeout=2)
            assert receipt is not None and receipt.idempotent_replay
            assert receipt.run_id == db.winner.id
            assert row_values(db.winner) == original
            assert "skill" not in db.events
        else:
            with pytest.raises(
                PublishedTaskNotFoundError, match=r"^Published task is not available$"
            ):
                await asyncio.wait_for(task, timeout=2)
            assert db.committed == [] and db.staged == []
            assert db.rollbacks == 1 and db.commits == 0
            db.session.add_all.assert_not_called()
        assert db.resolved is resolved
    finally:
        disabled.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["deprecated", "draft", "missing", "source-org", "skill-org", "project", "version"]
)
async def test_new_run_requires_exact_current_published_project_binding(change: str) -> None:
    """原解決済み snapshot があっても、別組織/別 Project/別版の有効化を借用しない。"""

    db = CreationAuthorizationHarness()
    if change in {"deprecated", "draft"}:
        db.version.status = change.upper()
    elif change == "missing":
        db.task_binding.binding = None
    elif change == "source-org":
        db.task_binding.source.organization_id = uuid4()
    elif change == "skill-org":
        db.task_binding.skill.organization_id = uuid4()
    else:
        assert db.task_binding.binding is not None
        if change == "project":
            db.task_binding.binding.project_id = uuid4()
        else:
            db.task_binding.binding.skill_version_id = uuid4()
    with pytest.raises(PublishedTaskNotFoundError):
        await db.call()
    assert db.committed == [] and db.staged == []
    assert db.commits == 0 and db.rollbacks == 1
    db.session.add_all.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["first-replay", "unique-winner", "find-hit"])
async def test_original_winner_is_replayed_without_current_version_lookup(path: str) -> None:
    """最初の照会/INSERT 唯一競争/専用照会とも、今日の廃棄で旧快照を拒否しない。"""

    db = CreationAuthorizationHarness(path)
    db.version.status = "DEPRECATED"
    db.task_binding.binding = None
    before = row_values(db.winner)
    result = await db.call()
    assert result is not None and result.idempotent_replay
    assert result.run_id == db.winner.id
    assert row_values(db.winner) == before
    assert not {"skill", "manifest"}.intersection(db.events)
    assert not any(
        getattr(statement, "column_descriptions", [{}])[0].get("entity")
        in (SkillVersion, ProjectSkillVersion)
        for statement in db.statements
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("entity", [SkillVersion, ProjectSkillVersion])
async def test_original_session_expiry_after_binding_wait_precedes_task_state(
    monkeypatch: pytest.MonkeyPatch, available: bool, entity: type[Any]
) -> None:
    """固定 expiry を跨ぐ時刻だけを進め、新規 gate 成否のどちらでも原会話を再検証する。"""

    db = CreationAuthorizationHarness()
    CreationClock.current = datetime.now(UTC)
    db.auth_session.idle_expires_at = CreationClock.current + timedelta(seconds=1)
    monkeypatch.setattr("projectmind.runs.service.datetime", CreationClock)
    if not available:
        if entity is SkillVersion:
            db.version.status = "DEPRECATED"
        else:
            db.task_binding.binding = None

    def advance(event: str) -> None:
        """保存行の期限を変えず、SQL 待機から戻る時刻だけを操作する。"""

        if event == f"lock:{entity.__name__}":
            CreationClock.current += timedelta(seconds=2)

    db.on_step = advance
    with pytest.raises(UnauthorizedSessionError):
        await db.call()
    assert db.committed == [] and db.staged == [] and db.rollbacks == 1


@pytest.mark.asyncio
async def test_cancelled_binding_wait_cannot_commit_initial_run() -> None:
    """共有 SELECT の取消は業務拒否に畳まず、仮 INSERT/event/outbox を全て回滚する。"""

    db = CreationAuthorizationHarness()

    def cancel(event: str) -> None:
        """実外部 task を残さず、await 境界の native 取消を注入する。"""

        if event == "lock:ProjectSkillVersion":
            raise asyncio.CancelledError

    db.on_step = cancel
    with pytest.raises(asyncio.CancelledError):
        await db.call()
    assert db.committed == [] and db.staged == [] and db.rollbacks == 1
