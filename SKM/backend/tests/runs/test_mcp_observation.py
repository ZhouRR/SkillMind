"""MCP の証拠補完と保存後の固定を実 SQLite JOIN で検証する。外部操作は行わない。"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.db.models import ChangeProposal, Evidence, ResourceBinding
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.proposal import parse_change_proposal_request
from skillmind.integrations.mcp_tools import digest
from tests.agent.test_mcp_tools import TOOLS, catalog, config
from tests.runs.test_database_observation import observation as observation


@pytest.fixture
def mcp_observation(observation):
    """既存の二表 fixture を使い、同 Run の既知の MCP 工具発見を登録する。"""
    h = observation
    h.binding.provider = "mcp"
    h.binding.requirement_key = "runner"
    h.binding.scope_json = {"resource_uris": [], "tool_names": sorted(TOOLS)}
    h.tool.provider, h.tool.capability_version = "mcp", "mcp.tools/v1"
    h.evidence.evidence_ref = "ev_catalog"
    h.evidence.evidence_type = "mcp"
    h.evidence.source_locator = {"catalog_hash": digest(catalog())}
    h.payload = {"name": "update_inventory", "catalog_hash": digest(catalog())}
    h.draft = parse_change_proposal_request({
        "resource_key": "runner", "capability_version": "mcp.call/v1", "operation": "call",
        "target": {"locator": "update_inventory", "display": "Inventory"},
        "changes": [{"path": "/call", "action": "SET", "value": {
            "arguments": {"item": "fixture", "quantity": 3},
            "read_back": {"name": "read_inventory", "arguments": {"item": "fixture"},
                          "checks": [{"path": "/quantity", "equals": 3}]},
        }}],
        "precondition": {"revision": digest(catalog())},
        "summary": "Update observed inventory.", "evidence_refs": ["ev_observation"],
    }, request_identity="fixture-call")
    return h


async def verify(h, *, lookup=True):
    """候補作成と保存済み提案の同じ照合処理を呼ぶ。"""
    return await h.repository._validate_mcp_observation(
        h.draft, binding=h.binding, payload=h.payload, allow_lookup=lookup,
    )


async def test_missing_reference_is_resolved_but_stored_proposals_require_original(mcp_observation):
    """発見済みなら補完可能だが、確定済み提案へ後から別証拠を注入しない。"""
    h = mcp_observation
    assert await verify(h) == "ev_catalog"
    with pytest.raises(ChangeProposalValidationError, match="original catalog Evidence"):
        await verify(h, lookup=False)
    h.draft = replace(h.draft, evidence_refs=(*h.draft.evidence_refs, "ev_catalog"))
    assert await verify(h, lookup=False) == "ev_catalog"


@pytest.mark.parametrize("change", [
    "evidence-run", "tool-run", "integration", "failed", "pending", "query-tool",
    "provider", "missing-tool", "binding", "catalog", "missing-binding", "missing-catalog",
])
async def test_lookup_never_borrows_untrusted_observation(mcp_observation, change):
    """番号の省略だけを補い、別 Run/接続/範囲/版や不成功の観測は採用しない。"""
    h = mcp_observation
    if change == "evidence-run":
        h.evidence.run_id = uuid4()
    elif change == "tool-run":
        h.tool.run_id = uuid4()
    elif change == "integration":
        h.tool.integration_id = uuid4()
    elif change in {"failed", "pending"}:
        h.tool.status = "FAILED" if change == "failed" else "RUNNING"
    elif change == "query-tool":
        h.tool.capability_version = "mcp.query/v1"
    elif change == "provider":
        h.tool.provider = "other"
    elif change == "missing-tool":
        h.evidence.tool_call_id = uuid4()
    elif change == "binding":
        h.evidence.metadata_json = {"binding_checksum": "another-binding"}
    elif change == "catalog":
        h.evidence.source_locator = {"catalog_hash": "sha256:" + "0" * 64}
    elif change == "missing-binding":
        h.evidence.metadata_json = {}
    else:
        h.evidence.source_locator = {}
    with pytest.raises(ChangeProposalValidationError, match="original catalog Evidence"):
        await verify(h)


async def test_repeated_discovery_prefers_explicit_reference_without_changing_observation(
    mcp_observation,
):
    """同版の再発見でも原観測は不変で、明示済み参照を優先する。"""
    h = mcp_observation
    original_time = h.evidence.created_at
    other = Evidence(
        id=uuid4(), evidence_ref="ev_catalog_later", run_id=h.binding.run_id,
        tool_call_id=h.tool.id, evidence_type="mcp", source_uri=h.evidence.source_uri,
        source_locator=dict(h.evidence.source_locator), content_hash=h.evidence.content_hash,
        metadata_json=dict(h.evidence.metadata_json),
        created_at=original_time + timedelta(seconds=1),
    )
    h.repository._session.session.add(other)
    assert await verify(h) == "ev_catalog"
    h.draft = replace(h.draft, evidence_refs=("ev_catalog_later",))
    assert await verify(h) == "ev_catalog_later"
    assert h.evidence.created_at == original_time
    # 批准後に原参照が失われても、別の成功観測へ黙って差し替えない。
    h.repository._session.session.delete(other)
    with pytest.raises(ChangeProposalValidationError):
        await verify(h, lookup=False)


@pytest.mark.parametrize("failure", [None, "revoked", "scope", "revision"])
async def test_draft_links_evidence_only_after_current_authorization_and_contract(
    mcp_observation, monkeypatch, failure,
):
    """実候補検証で参照だけを補完し、原要求・認可・契約・取消対象の検証を保つ。"""
    h = mcp_observation
    session = h.repository._session
    query = session.scalars

    async def scalars(statement):
        """保存先行は fixture、Evidence の絞り込みは実 SQL へ委譲する。"""
        entity = statement.column_descriptions[0].get("entity")
        if entity is ChangeProposal:
            return Mock(one_or_none=lambda: None)
        if entity is ResourceBinding:
            return Mock(one_or_none=lambda: h.binding)
        return await query(statement)

    monkeypatch.setattr(session, "scalars", scalars)
    integration = SimpleNamespace(config_json=config())
    authorize = AsyncMock(return_value=integration)
    if failure == "revoked":
        authorize.side_effect = ChangeProposalValidationError("revoked")
    elif failure == "scope":
        h.binding.scope_json = {"resource_uris": [], "tool_names": ["read_inventory"]}
    elif failure == "revision":
        h.draft = replace(h.draft, precondition={"revision": "sha256:" + "0" * 64})
    monkeypatch.setattr(h.repository, "_validate_effect_binding", authorize)
    monkeypatch.setattr(h.repository, "_validate_effect_artifact", AsyncMock())
    observation = AsyncMock(wraps=h.repository._validate_mcp_observation)
    monkeypatch.setattr(h.repository, "_validate_mcp_observation", observation)
    blueprint = {"effect_intents": [{
        "key": h.draft.effect_intent_key, "mode": "apply", "resource_key": "runner",
        "operation": "call", "risk": h.draft.risk_level.value,
    }]}
    claimed = SimpleNamespace(skill_snapshots_json=({"manifest": {
        "capability_blueprint": blueprint,
    }},))
    run = SimpleNamespace(id=h.binding.run_id, project_id=uuid4())
    if failure:
        with pytest.raises(ChangeProposalValidationError):
            await h.repository._validate_proposal_draft(claimed, run=run, draft=h.draft)
        observation.assert_not_awaited()
        return
    for _ in range(2):
        draft, _, _, _ = await h.repository._validate_proposal_draft(
            claimed, run=run, draft=h.draft,
        )
        assert draft.evidence_refs == ("ev_observation", "ev_catalog")
        assert draft.request_fingerprint == h.draft.request_fingerprint
        assert draft.idempotency_key == h.draft.idempotency_key
        assert draft.changes == h.draft.changes and draft.precondition == h.draft.precondition
        assert h.draft.evidence_refs == ("ev_observation",)
        # 同じ入力の復旧でも、補完済み候補でも番号を重複させない。
        again, _, _, _ = await h.repository._validate_proposal_draft(claimed, run=run, draft=draft)
        assert again.evidence_refs == draft.evidence_refs
    assert authorize.await_count == 4
