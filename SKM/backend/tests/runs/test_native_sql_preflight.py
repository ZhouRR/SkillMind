"""SQL 提案の事前検査が原 lease・取消・共有 validator を通り、状態を書き換えない。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.effects.postgres_native import SqlObservationValidationError
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.domain import LeaseValidationError, RunCancellationRequestedError
from skillmind.runs.repository import RunRepository
from skillmind.runs.service import RunService
from tests.effects.test_native_resource_effects import sql_execution
from tests.runs.test_execution_gates import execution_rows


@pytest.mark.parametrize("mode", ["valid", "observation", "expired", "cancelled"])
async def test_service_checks_proposal_under_original_lease_without_mutation(monkeypatch, mode):
    """実 Service/Repository でロック・期限・取消を検査し、保存処理を実行しない。"""
    claimed, run, segment, attempt = execution_rows()
    execution = sql_execution()
    arguments = {
        "resource_key": "records",
        "capability_version": execution.capability_version,
        "operation": execution.operation,
        "target": execution.target,
        "changes": list(execution.changes),
        "precondition": execution.precondition,
        "summary": "Update record",
        "evidence_refs": ["ev_original"],
    }
    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    transaction_active = False

    @asynccontextmanager
    async def transaction():
        """原実行 lock と検査を同じ transaction に置く。"""
        nonlocal transaction_active
        transaction_active = True
        try:
            yield
        finally:
            transaction_active = False

    session.begin.side_effect = transaction
    repository = RunRepository(session, execution_features=ExecutionFeatures(database_writes=True))

    async def lock(actual):
        """同じ Run/Attempt の lock 取得後にだけ検査へ進む。"""
        assert transaction_active and actual == claimed
        return run, segment, attempt

    monkeypatch.setattr(repository, "_lock_claimed_execution", lock)
    validate = AsyncMock(
        side_effect=SqlObservationValidationError("bad") if mode == "observation" else None
    )
    evidence = AsyncMock()
    monkeypatch.setattr(repository, "_validate_proposal_draft", validate)
    monkeypatch.setattr(repository, "_validate_evidence_refs", evidence)
    session.scalar.return_value = "cancel-event" if mode == "cancelled" else None
    if mode == "expired":
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr("skillmind.runs.service.RunRepository", lambda *args, **kwargs: repository)
    service = RunService(MagicMock(return_value=session), database_writes_enabled=True)
    call = service.validate_native_sql_proposal(claimed, arguments, tool_use_id="original-call")
    if mode == "valid":
        await call
        draft = validate.await_args.kwargs["draft"]
        assert draft.capability_version == execution.capability_version
        assert draft.changes == tuple(arguments["changes"])
        evidence.assert_awaited_once_with(run.id, ("ev_original",))
    else:
        expected = {
            "observation": SqlObservationValidationError,
            "expired": LeaseValidationError,
            "cancelled": RunCancellationRequestedError,
        }[mode]
        with pytest.raises(expected):
            await call
        evidence.assert_not_awaited()
        if mode != "observation":
            validate.assert_not_awaited()
    assert run.status == "RUNNING" and segment.status == "RUNNING"
    assert attempt.status == "RUNNING" and not transaction_active
    session.add.assert_not_called()
    session.add_all.assert_not_called()


async def test_other_effects_do_not_open_extra_preflight_transaction():
    """今回の SQL 修正で別 Provider の保存待ち・追加 DB 呼出しを増やさない。"""
    sessions = MagicMock()
    await RunService(sessions).validate_native_sql_proposal(
        execution_rows()[0],
        {"capability_version": "document.write/v1"},
        tool_use_id="document-call",
    )
    sessions.assert_not_called()
