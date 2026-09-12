"""DB の独立上限を capability/operation と保存済み effect の入口で検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skillmind.effects.domain import ApprovalDecision, DecideProposalCommand
from skillmind.effects.release import ExecutionFeatureDisabledError, ExecutionFeatures
from tests.runs.effect_authorization_harness import AuthorizationHarness


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("database", [False, True])
def test_database_and_deferred_capabilities_have_independent_limits(deferred, database):
    """後置 switch では DB が開かず、DB switch では他 write と子が開かない。"""
    features = ExecutionFeatures(deferred, database)
    assert features.effect_enabled("database.write/v1", "INSERT") is database
    assert features.effect_enabled("database.write/v1", "UPDATE") is database
    assert not features.effect_enabled("database.write/v1", "DELETE")
    assert not features.effect_enabled("unregistered.write/v1", "INSERT")
    assert features.effect_enabled("issue.update/v1", "update_fields") is deferred
    assert features.capability_enabled("subagent.dispatch/v1") is deferred
    assert features.capability_enabled("change.propose/v1") is (deferred or database)
    assert features.capability_enabled("workspace.write/v1")
    assert features.capability_enabled("database.read/v1")


@pytest.mark.parametrize(
    "capability,operation,expected",
    [
        ("database.write/v1", "INSERT", True),
        ("database.write/v1", "UPDATE", True),
        ("database.write/v1", "DELETE", False),
        ("database.write/v2", "INSERT", False),
        ("repository.write/v1", "commit", False),
        ("issue.update/v1", "update_fields", False),
    ],
)
def test_database_only_run_requires_exact_blueprint_resource_and_operation(
    capability, operation, expected
):
    """apply intent の自由文で別の Provider や SQL 操作へ退避できない。"""
    blueprint = {
        "resource_requirements": [{"key": "data", "capabilities": [capability]}],
        "effect_intents": [{"mode": "apply", "resource_key": "data", "operation": operation}],
    }
    assert ExecutionFeatures(database_writes=True).blueprint_enabled(blueprint) is expected
    blueprint["effect_intents"][0]["resource_key"] = "missing"
    assert not ExecutionFeatures(database_writes=True).blueprint_enabled(blueprint)


def scalar_result(statement, h):
    """元 proposal/execution 行を返す double。lock の実動作は別途検証する。"""
    entity = statement.column_descriptions[0]["entity"]
    result = MagicMock()
    result.one.return_value = h.rows[entity]
    result.one_or_none.return_value = h.rows[entity]
    return result


@pytest.mark.parametrize(
    "capability,operation,features",
    [
        ("database.write/v1", "INSERT", ExecutionFeatures(deferred=True)),
        ("database.write/v1", "DELETE", ExecutionFeatures(database_writes=True)),
        ("issue.update/v1", "update_fields", ExecutionFeatures(database_writes=True)),
    ],
)
async def test_old_queue_cannot_claim_an_effect_outside_current_deployment(
    capability, operation, features
):
    """要求本文でなく保存済み proposal を調べ、claim の状態更新前に閉じる。"""
    h = AuthorizationHarness()
    h.repository._execution_features = features
    h.proposal.capability_version, h.proposal.operation = capability, operation
    h.session.scalars = AsyncMock(side_effect=lambda statement: scalar_result(statement, h))
    with pytest.raises(ExecutionFeatureDisabledError):
        await h.repository.claim_effect_execution(
            h.claimed.effect_execution_id,
            worker_id="test-worker",
            lease_token="unit-test",
            lease_token_hash_value="a" * 64,
            lease_seconds=60,
            max_attempts=3,
        )
    assert h.execution.attempt_no == 1
    h.session.add.assert_not_called()
    h.session.flush.assert_not_called()


async def test_database_only_approval_uses_stored_capability_before_creating_approval():
    """DB switch を用いて既存 Redmine 提案を批准する経路を拒否する。"""
    h = AuthorizationHarness()
    h.proposal.capability_version = "issue.update/v1"
    h.proposal.operation = "update_fields"
    h.session.scalars = AsyncMock(side_effect=lambda statement: scalar_result(statement, h))
    command = DecideProposalCommand(
        project_id=h.claimed.project_id,
        run_id=h.claimed.run_id,
        proposal_id=h.claimed.proposal_id,
        actor_id=h.actor.id,
        actor_is_administrator=False,
        decision=ApprovalDecision.APPROVED,
        proposal_version=1,
        proposal_checksum=h.proposal.checksum,
        idempotency_key="test:decision:001",
        reason="Reviewed",
        trace_id=None,
    )
    with pytest.raises(ExecutionFeatureDisabledError):
        await h.repository.decide_change_proposal(command)
    h.session.add.assert_not_called()
    h.session.flush.assert_not_called()
