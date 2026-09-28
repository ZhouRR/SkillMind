"""実行履歴/詳細が現行 catalog でなく原 Skill の名称を返す境界を確認する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.runs.domain import derive_task_id
from skillmind.runs.repository import RunRepository


@pytest.mark.asyncio
async def test_title_projection_requires_original_version_and_exact_task() -> None:
    """廃止した版も参照し、同名 key の別版や別 task の名称は流用しない。"""
    version_id, run_id = uuid4(), uuid4()
    run = SimpleNamespace(
        id=run_id, task_id=derive_task_id(skill_version_id=version_id, task_key="review")
    )
    manifest = SimpleNamespace(manifest_json={
        "tasks": [
            {"key": "other", "capability": "other"},
            {"key": "review", "capability": "review"},
        ],
        "capabilities": [
            {"key": "other", "title": "Other"},
            {"key": "review", "title": "Original review"},
        ],
    })
    snapshot = SimpleNamespace(run_id=run_id, skill_version_id=version_id)
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(all=lambda: [(snapshot, manifest)]))
    repository = RunRepository(session)
    assert await repository._history_task_titles([run]) == {run_id: "Original review"}
    query = str(session.execute.call_args.args[0])
    assert "manifest_checksum" in query and "checksum" in query
    assert "PUBLISHED" not in query
    snapshot.skill_version_id = uuid4()
    assert await repository._history_task_titles([run]) == {}
