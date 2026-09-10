"""EvaluationRepository の ownership、追加性、Result 不変性を検証する。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import Evaluation, RunResult
from skillmind.evaluations.domain import (
    CreateEvaluationCommand,
    EvaluationRevisionProposal,
    EvaluationVerdict,
    InvalidEvaluationRevisionError,
)
from skillmind.evaluations.repository import EvaluationRepository
from skillmind.runs.domain import RunNotFoundError


def _result(run_id: object) -> RunResult:
    """Repository test 用の不変 Result row を生成する。"""

    from uuid import UUID

    assert isinstance(run_id, UUID)
    return RunResult(
        id=uuid4(),
        run_id=run_id,
        agent_session_id=uuid4(),
        output_schema="test/v1",
        result_kind="STRUCTURED_OUTPUT",
        data_json={"issue": {"id": "ISSUE-1"}, "fields": [{"value": "before"}]},
        evidence_refs_json=[],
        artifact_refs_json=[],
        optional_schema_identity_json={"schema_ref": "test/v1"},
        summary="summary",
        confidence=0.8,
        needs_review=False,
        usage_json={},
        cost_json={},
        validation_json={"schema_valid": True},
        created_at=datetime.now(UTC),
    )


def _command(*, project_id: object, run_id: object) -> CreateEvaluationCommand:
    """一 field revision を含む有効な Evaluation command を生成する。"""

    from uuid import UUID

    assert isinstance(project_id, UUID)
    assert isinstance(run_id, UUID)
    return CreateEvaluationCommand(
        project_id=project_id,
        run_id=run_id,
        user_id=uuid4(),
        rating=4,
        verdict=EvaluationVerdict.PARTIALLY_ACCURATE,
        comment="Needs a field correction",
        revisions=(
            EvaluationRevisionProposal(
                pointer="/fields/0/value",
                suggested_value="after",
                reason="Reviewed against evidence",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_create_derives_original_value_without_mutating_result() -> None:
    """原値を Server 側で固定し、評価追加後も Result JSON を変更しない。"""

    project_id = uuid4()
    run_id = uuid4()
    result = _result(run_id)
    original_data = {"issue": {"id": "ISSUE-1"}, "fields": [{"value": "before"}]}
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[run_id, result])

    stored = await EvaluationRepository(session).create(
        _command(project_id=project_id, run_id=run_id)
    )

    assert stored.revisions[0].original_value == "before"
    assert stored.revisions[0].suggested_value == "after"
    assert result.data_json == original_data
    added = session.add.call_args.args[0]
    assert isinstance(added, Evaluation)
    assert added.revision_json[0]["original_value"] == "before"


@pytest.mark.asyncio
async def test_create_rejects_duplicate_revision_pointer() -> None:
    """同一評価内の pointer 重複を拒否して表示優先順位の曖昧さを防ぐ。"""

    project_id = uuid4()
    run_id = uuid4()
    result = _result(run_id)
    command = _command(project_id=project_id, run_id=run_id)
    duplicate = CreateEvaluationCommand(
        project_id=command.project_id,
        run_id=command.run_id,
        user_id=command.user_id,
        rating=command.rating,
        verdict=command.verdict,
        comment=command.comment,
        revisions=(command.revisions[0], command.revisions[0]),
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[run_id, result])

    with pytest.raises(InvalidEvaluationRevisionError, match="unique"):
        await EvaluationRepository(session).create(duplicate)
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_cross_project_create_fails_closed() -> None:
    """Project に属さない Run の Result 存在有無を Evaluation API へ漏らさない。"""

    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)

    with pytest.raises(RunNotFoundError):
        await EvaluationRepository(session).create(
            _command(project_id=uuid4(), run_id=uuid4())
        )
    assert session.scalar.await_count == 1
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_list_returns_evaluations_in_repository_order() -> None:
    """Project ownership 確認後の履歴が保存済み原値を失わず read model へ戻る。"""

    project_id = uuid4()
    run_id = uuid4()
    result = _result(run_id)
    evaluation = Evaluation(
        id=uuid4(),
        result_id=result.id,
        user_id=uuid4(),
        rating=5,
        verdict=EvaluationVerdict.ACCURATE.value,
        comment="confirmed",
        revision_json=[
            {
                "pointer": "/fields/0/value",
                "original_value": "before",
                "suggested_value": "after",
                "reason": "manual review",
            }
        ],
        created_at=datetime.now(UTC),
    )
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(side_effect=[run_id, result])
    rows = MagicMock()
    rows.all.return_value = [evaluation]
    session.scalars = AsyncMock(return_value=rows)

    stored = await EvaluationRepository(session).list_for_run(
        project_id=project_id,
        run_id=run_id,
    )

    assert stored[0].evaluation_id == evaluation.id
    assert stored[0].revisions[0].original_value == "before"


@pytest.mark.asyncio
async def test_concurrent_creates_append_distinct_evaluations() -> None:
    """同じ Result への並行評価が競合更新せず別 UUID で追加される。"""

    project_id = uuid4()
    run_id = uuid4()
    result = _result(run_id)
    first_session = MagicMock(spec=AsyncSession)
    first_session.scalar = AsyncMock(side_effect=[run_id, result])
    second_session = MagicMock(spec=AsyncSession)
    second_session.scalar = AsyncMock(side_effect=[run_id, result])

    first, second = await asyncio.gather(
        EvaluationRepository(first_session).create(
            _command(project_id=project_id, run_id=run_id)
        ),
        EvaluationRepository(second_session).create(
            _command(project_id=project_id, run_id=run_id)
        ),
    )

    assert first.evaluation_id != second.evaluation_id
    first_session.add.assert_called_once()
    second_session.add.assert_called_once()
