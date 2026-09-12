"""ChangeProposal decision と effect preauthorization API contract を検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from skillmind.api.routes.effects import ChangeProposalResponse
from tests.api.fakes import FakeEffectService, FakeProposalDecisionService


async def _library_decision(service, command, *, access):
    """承認ロジックは mock に限定し、実 response projection に内部文書庫の形を渡す。"""

    result = await FakeProposalDecisionService.decide_change_proposal(
        service, command, access=access
    )
    return replace(
        result,
        proposal=replace(
            result.proposal,
            integration_id=None,
            capability_version="document.write/v1",
            operation="CREATE",
            target={"locator": "rv/fixture/source.md"},
            summary="Save the original Artifact",
        ),
    )


def test_document_proposal_response_preserves_null_and_rejects_other_null_targets(
    client, monkeypatch
):
    """実 HTTP serializer、response model と共有 detail Schema が同じ NULL 境界を持つ。"""

    service = FakeProposalDecisionService()
    monkeypatch.setattr(
        service,
        "decide_change_proposal",
        lambda command, access: _library_decision(service, command, access=access),
    )
    client.app.state.run_service = service
    response = client.post(
        f"/api/v1/projects/{uuid4()}/runs/{uuid4()}/proposals/{uuid4()}/decision",
        headers={"Idempotency-Key": "library-response"},
        json={
            "decision": "APPROVED",
            "proposal_version": 1,
            "proposal_checksum": "sha256:" + "a" * 64,
            "reason": "Reviewed",
        },
    )
    assert response.status_code == 200
    value = response.json()["proposal"]
    assert value["integration_id"] is None
    path = Path(__file__).resolve().parents[3] / "contracts/runs/detail/v1.schema.json"
    schema = Draft202012Validator(
        json.loads(path.read_text())["properties"]["change_proposals"]["items"]
    )
    assert schema.is_valid(value)
    for changes in (
        {"capability_version": "database.write/v1"},
        {"integration_id": str(uuid4())},
        {"operation": "UPDATE"},
    ):
        with pytest.raises(ValidationError):
            ChangeProposalResponse.model_validate({**value, **changes})
        assert not schema.is_valid({**value, **changes})


def test_admin_decision_freezes_proposal_version_checksum_and_idempotency(
    client: TestClient,
) -> None:
    """Exact Proposal 表示内容と actor authority を decision command へ渡す。"""

    service = FakeProposalDecisionService()
    client.app.state.run_service = service
    project_id = uuid4()
    run_id = uuid4()
    proposal_id = uuid4()
    checksum = "sha256:" + ("a" * 64)
    response = client.post(
        (
            f"/api/v1/projects/{project_id}/runs/{run_id}/proposals/"
            f"{proposal_id}/decision"
        ),
        headers={"Idempotency-Key": "proposal-decision-api-001"},
        json={
            "decision": "APPROVED",
            "proposal_version": 4,
            "proposal_checksum": checksum,
            "reason": "Reviewed exact target and field patch.",
        },
    )

    assert response.status_code == 200
    assert response.json()["proposal"]["status"] == "APPROVED"
    assert response.json()["approval"]["proposal_checksum"] == checksum
    assert service.received is not None
    assert service.received_access.actor.user_id == service.received.actor_id
    assert service.received.actor_is_administrator is True
    assert service.received.proposal_version == 4
    assert service.received.proposal_checksum == checksum
    assert service.received.idempotency_key == "proposal-decision-api-001"


def test_admin_creates_lists_and_disables_low_risk_preauthorization(
    client: TestClient,
) -> None:
    """LOW-only exact policy を optimistic version 付きで無効化できる。"""

    service = FakeEffectService()
    client.app.state.effect_service = service
    project_id = uuid4()
    created = client.post(
        f"/api/v1/projects/{project_id}/effect-preauthorizations",
        json={
            "integration_id": str(uuid4()),
            "capability_version": "issue.update/v1",
            "operation": "update_fields",
            "risk_level": "LOW",
            "scope": {"issue_ids": ["1001"], "field_keys": ["status_id"]},
            "expires_at": "2026-07-19T00:00:00Z",
        },
    )

    assert created.status_code == 201
    assert created.json()["max_risk_level"] == "LOW"
    listed = client.get(
        f"/api/v1/projects/{project_id}/effect-preauthorizations"
    )
    assert listed.status_code == 200
    assert listed.json()["items"] == [created.json()]

    disabled = client.post(
        (
            f"/api/v1/projects/{project_id}/effect-preauthorizations/"
            f"{created.json()['preauthorization_id']}/disable"
        ),
        json={"expected_policy_version": 1},
    )

    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    assert disabled.json()["policy_version"] == 2


def test_medium_risk_cannot_be_preauthorized(client: TestClient) -> None:
    """ADMIN でも LOW 以外の effect policy を構成できない。"""

    client.app.state.effect_service = FakeEffectService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/effect-preauthorizations",
        json={
            "integration_id": str(uuid4()),
            "capability_version": "issue.update/v1",
            "operation": "update_fields",
            "risk_level": "MEDIUM",
            "scope": {"issue_ids": ["1001"], "field_keys": ["status_id"]},
            "expires_at": None,
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "controlled_effect_rejected"


def test_wildcard_scope_cannot_be_preauthorized(client: TestClient) -> None:
    """Integration scope が wildcard でも、無人 apply の policy は逐項列挙を要求する。"""

    client.app.state.effect_service = FakeEffectService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/effect-preauthorizations",
        json={
            "integration_id": str(uuid4()),
            "capability_version": "issue.update/v1",
            "operation": "update_fields",
            "risk_level": "LOW",
            "scope": {"issue_ids": ["*"], "field_keys": ["status_id"]},
            "expires_at": None,
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "controlled_effect_rejected"
