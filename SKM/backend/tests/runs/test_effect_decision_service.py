"""ChangeProposal decision service の expiry commit 境界を検証する。"""

from __future__ import annotations

from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import ChangeProposal, Run
from skillmind.effects.domain import (
    ApprovalDecision,
    ChangeProposalApprovalForbiddenError,
    ChangeProposalExpiredError,
    DecideProposalCommand,
)
from skillmind.runs.repository import RunRepository
from skillmind.runs.service import RunService


class RecordingTransaction:
    """Transaction block から例外が出たかを記録する context。"""

    def __init__(self) -> None:
        """未終了状態で初期化する。"""

        self.exception_type: type[BaseException] | None | object = _NOT_EXITED

    async def __aenter__(self) -> Self:
        """自分自身を返す。"""

        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> None:
        """Commit/rollback 判定に使われる例外型を記録する。"""

        del exception, traceback
        self.exception_type = exception_type


_NOT_EXITED = object()


class RecordingSession:
    """RunService が要求する session/transaction context を提供する。"""

    def __init__(self) -> None:
        """一つの transaction recorder を作る。"""

        self.transaction = RecordingTransaction()

    async def __aenter__(self) -> Self:
        """自分自身を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Session close は test では何もしない。"""

        del args

    def begin(self) -> RecordingTransaction:
        """記録対象 transaction を返す。"""

        return self.transaction


class SessionFactory:
    """同じ RecordingSession を返す async session factory。"""

    def __init__(self) -> None:
        """検証可能な session を保持する。"""

        self.session = RecordingSession()

    def __call__(self) -> RecordingSession:
        """RunService 用 session を返す。"""

        return self.session


@pytest.mark.asyncio
async def test_expiry_state_is_committed_before_api_error_is_raised() -> None:
    """Expiry event/continuation を rollback せず、commit 後に 409 用例外を再送出する。"""

    factory = SessionFactory()

    async def expire(
        self: RunRepository, command: DecideProposalCommand
    ) -> None:
        """Repository が expiry rows を更新済みで例外を返す状況を再現する。"""

        del self, command
        raise ChangeProposalExpiredError("approval expired")

    command = DecideProposalCommand(
        project_id=uuid4(),
        run_id=uuid4(),
        proposal_id=uuid4(),
        actor_id=uuid4(),
        actor_is_administrator=False,
        decision=ApprovalDecision.APPROVED,
        proposal_version=1,
        proposal_checksum="sha256:" + ("a" * 64),
        idempotency_key="decision:test:0001",
        reason="Reviewed exact proposal",
        trace_id="trace-test",
    )

    with (
        patch.object(RunRepository, "decide_change_proposal", new=expire),
        pytest.raises(ChangeProposalExpiredError),
    ):
        await RunService(factory).decide_change_proposal(command)  # type: ignore[arg-type]

    assert factory.session.transaction.exception_type is None


@pytest.mark.asyncio
async def test_project_member_cannot_approve_another_actors_run() -> None:
    """Project membership だけでは外部 effect の批准権を付与しない。"""

    initiating_actor_id = uuid4()
    deciding_actor_id = uuid4()
    run_id = uuid4()
    project_id = uuid4()
    proposal = MagicMock(spec=ChangeProposal)
    proposal.run_segment_id = uuid4()
    run = MagicMock(spec=Run)
    run.permission_snapshot_json = {"actor_id": str(initiating_actor_id)}
    result_proposal = MagicMock()
    result_proposal.one_or_none.return_value = proposal
    result_run = MagicMock()
    result_run.one_or_none.return_value = run
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(side_effect=[result_proposal, result_run])
    command = DecideProposalCommand(
        project_id=project_id,
        run_id=run_id,
        proposal_id=uuid4(),
        actor_id=deciding_actor_id,
        actor_is_administrator=False,
        decision=ApprovalDecision.APPROVED,
        proposal_version=1,
        proposal_checksum="sha256:" + ("a" * 64),
        idempotency_key="decision:foreign-actor:0001",
        reason="Attempted cross-actor approval",
        trace_id="trace-test",
    )

    with pytest.raises(ChangeProposalApprovalForbiddenError):
        await RunRepository(session).decide_change_proposal(command)

    assert session.scalars.await_count == 2

