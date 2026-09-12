"""実 HTTP と提案 repository で配備上限を検証する。SQL/Artifact は合成する。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skillmind.db.models import (
    AuthSession,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    User,
    UserInteraction,
)
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.repository import RunRepository
from skillmind.runs.service import RunService
from tests.runs.test_document_proposals import document_proposal
from tests.runs.test_interaction_authorization import AuthorizationDatabase


async def prepare_approval(client, monkeypatch, features):
    """HTTP→service→repository と原認証/提案検証を残し、SQL/Artifact だけを合成する。"""

    harness = await document_proposal(monkeypatch)
    harness.proposal.status = "PENDING_APPROVAL"
    harness.proposal.created_at = harness.proposal.updated_at = datetime.now(UTC)
    harness.rows[ChangeApproval] = None
    harness.rows[EffectExecution] = None
    harness.rows[UserInteraction] = SimpleNamespace(
        status="OPEN", version=1, expires_at=harness.proposal.expires_at
    )
    database = AuthorizationDatabase()
    database.run, database.segment = harness.run, harness.segment
    database.interaction = harness.rows[UserInteraction]
    database.project.id = database.member.project_id = harness.run.project_id
    harness.run.permission_snapshot_json["actor_id"] = str(database.user.id)
    database.session.get = harness.session.get
    harness.session = database.session
    harness.database = database
    monkeypatch.setattr(RunRepository, "_validate_evidence_refs", AsyncMock())

    async def scalars(statement):
        """保存済み提案と既存判断だけを返し、公開 request から能力を補わない。"""

        entity = statement.column_descriptions[0]["entity"]
        if entity in (User, AuthSession):
            return await database.scalars(statement)
        row = harness.rows[entity]
        return SimpleNamespace(one=lambda: row, one_or_none=lambda: row)

    database.session.scalars = AsyncMock(side_effect=scalars)
    client.app.state.run_service = RunService(
        lambda: database.session,
        deferred_features_enabled=features.deferred,
        database_writes_enabled=features.database_writes,
        document_writes_enabled=features.document_writes,
        document_library_target=harness.target,
    )
    auth = client.app.state.auth_service
    auth.actor = database.access.actor
    auth.session_token = database.access.session_token
    auth.csrf_token = database.access.csrf_token
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, auth.session_token)
    client.headers["X-CSRF-Token"] = auth.csrf_token
    return harness


def post_decision(client, harness, *, decision="APPROVED"):
    """元提案 version/checksum と同じ request key を HTTP に送る。"""

    return client.post(
        f"/api/v1/projects/{harness.run.project_id}/runs/{harness.run.id}"
        f"/proposals/{harness.proposal.id}/decision",
        headers={"Idempotency-Key": "original-library-approval"},
        json={
            "decision": decision,
            "proposal_version": harness.proposal.version,
            "proposal_checksum": harness.proposal.checksum,
            "reason": "Reviewed original Markdown and destination",
        },
    )


@pytest.mark.parametrize(
    "features,capability,operation,expected",
    [
        (ExecutionFeatures(document_writes=True), "document.write/v1", "CREATE", 200),
        (ExecutionFeatures(), "document.write/v1", "CREATE", 409),
        (ExecutionFeatures(database_writes=True), "document.write/v1", "CREATE", 409),
        (ExecutionFeatures(deferred=True), "document.write/v1", "CREATE", 409),
        (ExecutionFeatures(document_writes=True), "document.write/v1", "UPDATE", 409),
        (ExecutionFeatures(document_writes=True), "database.write/v1", "INSERT", 409),
        (ExecutionFeatures(document_writes=True), "issue.update/v1", "update_fields", 409),
    ],
)
async def test_approval_uses_original_proposal_and_independent_capability_limit(
    client, monkeypatch, features, capability, operation, expected
):
    """旧 switch は双方閉じ、内部文書庫の独立上限だけで原 CREATE を批准できる。"""

    harness = await prepare_approval(client, monkeypatch, features)
    harness.proposal.capability_version = capability
    harness.proposal.operation = operation
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False, "database_writes_enabled": False}
    )
    response = post_decision(client, harness)
    assert response.status_code == expected
    if expected == 409:
        assert response.json()["code"] == "feature_not_enabled"
        assert response.headers["cache-control"] == "no-store"
        assert harness.proposal.status == "PENDING_APPROVAL"
        assert not harness.database.transaction.committed
        harness.session.add_all.assert_not_called()
        harness.read_artifact.assert_not_awaited()
        return
    result = response.json()
    assert harness.database.transaction.committed
    assert result["proposal"]["integration_id"] is None
    assert result["proposal"]["status"] == "APPROVED"
    assert result["approval"]["proposal_checksum"] == harness.proposal.checksum
    assert result["effect_execution"]["provider"] == "project-library"
    assert result["effect_execution"]["status"] == "REQUESTED"
    staged = harness.session.add_all.call_args.args[0]
    assert sum(isinstance(row, ChangeApproval) for row in staged) == 1
    assert sum(isinstance(row, EffectExecution) for row in staged) == 1
    harness.read_artifact.assert_awaited_once()


async def test_disabled_deployment_does_not_disclose_missing_proposal_as_a_feature_error(
    client, monkeypatch
):
    """権限付き原対象の探索を先に行い、存在しない提案は同じ 404 にする。"""

    harness = await prepare_approval(client, monkeypatch, ExecutionFeatures())
    harness.rows[ChangeProposal] = None
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False, "database_writes_enabled": False}
    )
    response = post_decision(client, harness)
    assert response.status_code == 404
    harness.session.add_all.assert_not_called()


async def test_disabled_writes_still_allow_rejecting_the_original_proposal(client, monkeypatch):
    """文書 write を閉じても原拒否と次 Segment のみを保存し、Effect は作らない。"""

    harness = await prepare_approval(client, monkeypatch, ExecutionFeatures())
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False, "database_writes_enabled": False}
    )
    harness.run.row_version = 1
    harness.run.started_at = harness.proposal.created_at
    harness.run.finished_at = None
    harness.segment.segment_no = 1
    response = post_decision(client, harness, decision="REJECTED")
    assert response.status_code == 200
    assert response.json()["proposal"]["status"] == "REJECTED"
    assert response.json()["effect_execution"] is None
    assert harness.database.transaction.committed
    assert not any(isinstance(row, EffectExecution) for row in harness.database.rows)


async def test_expired_session_after_approval_staging_rolls_back_http_decision(client, monkeypatch):
    """配備上限を通過しても flush 待機後の会話失効で承認/dispatch を確定しない。"""

    harness = await prepare_approval(client, monkeypatch, ExecutionFeatures(document_writes=True))
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"deferred_features_enabled": False, "database_writes_enabled": False}
    )
    harness.database.on_flush = lambda: harness.database.invalidate("idle")
    response = post_decision(client, harness)
    assert response.status_code == 401
    harness.session.add_all.assert_called_once()
    assert not harness.database.transaction.committed
    assert harness.database.rows == []
