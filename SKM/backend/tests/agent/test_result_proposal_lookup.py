"""結果の Proposal 帰属と状態を同じ一回の SQL で読む。拒否条件を削らない。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from skillmind.agent.result_validation import PostgresProposalLookup, ProposalReferenceState
from sqlalchemy.dialects.postgresql import dialect


@pytest.mark.parametrize(
    "refs", [frozenset(), frozenset({"cp_applied", "cp_waiting", "cp_missing"})]
)
async def test_proposal_lookup_is_single_snapshot_and_scoped(refs):
    """空集合は DB を開かず、非空は必要な二列だけ同じ Run から読む。"""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [
        ("cp_applied", "APPLIED"),
        ("cp_waiting", "APPROVED"),
    ]
    session.execute.return_value = result
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    run_id = uuid4()
    actual = await PostgresProposalLookup(factory).inspect_refs(run_id, refs)
    if not refs:
        assert actual == ProposalReferenceState(frozenset(), frozenset())
        factory.assert_not_called()
        return
    assert actual.existing == {"cp_applied", "cp_waiting"}
    assert actual.incomplete == {"cp_waiting"}
    session.execute.assert_awaited_once()
    compiled = session.execute.await_args.args[0].compile(dialect=dialect())
    sql = str(compiled)
    assert "change_proposals.run_id" in sql
    assert "change_proposals.proposal_ref IN" in sql
    assert "preview_json" not in sql and "target_json" not in sql
    assert run_id in compiled.params.values()
