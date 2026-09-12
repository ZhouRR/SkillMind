"""原文書庫 binding と Artifact を、提案/批准/段階認可の共有入口で検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.artifacts.domain import ArtifactContent, ArtifactMetadata
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.core.hashing import sha256_hex
from skillmind.db.models import ChangeProposal, EffectExecution, Integration, ResourceBinding
from skillmind.documents.library import (
    DocumentLibraryBindingRepository,
    FrozenDocumentLibraryBinding,
)
from skillmind.effects.document_write import (
    DOCUMENT_WRITE_PROVIDER_VERSION,
    LEGACY_DOCUMENT_WRITE_PROVIDER_VERSION,
)
from skillmind.effects.domain import ChangeProposalValidationError, EffectLeaseValidationError
from skillmind.effects.proposal import (
    parse_change_proposal_request,
    proposal_checksum,
    proposal_content,
)
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.domain import lease_token_hash
from tests.documents.test_document_library_binding import target
from tests.effects.test_document_write import request
from tests.runs.effect_authorization_harness import AuthorizationHarness


async def document_proposal(monkeypatch, *, revision="2"):
    """SQL/外部 I/O を double にし、原 checksum と Artifact byte の本番 validator を通す。"""

    h = AuthorizationHarness()
    h.target = target()
    now = datetime.now(UTC)
    content = b"# Review\n"
    value = request()
    value["changes"][0]["value"].update(
        content_hash=f"sha256:{sha256_hex(content)}", size_bytes=len(content)
    )
    h.draft = replace(parse_change_proposal_request(value), expires_at=now + timedelta(minutes=2))
    h.artifact = ArtifactContent(
        ArtifactMetadata(
            artifact_ref="art_fixture",
            project_id=h.claimed.project_id,
            run_id=h.claimed.run_id,
            tool_call_id=uuid4(),
            evidence_ref="ev_artifact",
            path="output/source.md",
            size_bytes=len(content),
            mime_type="text/plain",
            checksum=f"sha256:{sha256_hex(content)}",
            created_at=now,
        ),
        content,
    )
    h.read_artifact = AsyncMock(return_value=h.artifact)
    monkeypatch.setattr(ArtifactRepository, "get_content", h.read_artifact)
    h.session.flush = AsyncMock()
    h.binding = await DocumentLibraryBindingRepository(h.session, target=h.target).freeze(
        project_id=h.run.project_id,
        run_id=h.run.id,
        actor_id=h.actor.id,
        requirement_key=h.draft.resource_key,
    )
    frozen = FrozenDocumentLibraryBinding(
        h.run.project_id, h.run.id, h.binding.id, h.draft.resource_key, h.target, revision
    ).to_json()
    # 保存済み旧版はその版の元 scope/checksum を持つ。新 freeze は常に v2 のまま。
    h.binding.revision, h.binding.scope_json = revision, frozen["scope"]
    h.binding.checksum = frozen["binding_checksum"]
    h.run.selected_sources_json = {h.draft.resource_key: frozen}
    h.claimed = replace(
        h.claimed,
        integration_id=None,
        binding_id=h.binding.id,
        capability_version="document.write/v1",
        operation="CREATE",
        target=deepcopy(h.draft.target),
        changes=deepcopy(h.draft.changes),
        precondition=deepcopy(h.draft.precondition),
        verification=deepcopy(h.draft.verification),
        provider="project-library",
        integration_revision=int(revision),
        integration_scope=h.target.scope(h.run.project_id, revision=revision),
        integration_config={},
        secret_reference_id=None,
        idempotency_key=h.draft.idempotency_key,
        request_fingerprint=h.draft.request_fingerprint,
    )
    p = h.proposal
    p.integration_id, p.target_binding_id = None, h.binding.id
    p.skill_version_id, p.effect_intent_key = uuid4(), h.draft.effect_intent_key
    p.capability_version, p.operation = "document.write/v1", "CREATE"
    p.target_json, p.preview_json = (
        deepcopy(h.draft.target),
        {"changes": deepcopy(list(h.draft.changes))},
    )
    p.precondition_json, p.verification_json = (
        deepcopy(h.draft.precondition),
        deepcopy(h.draft.verification),
    )
    p.summary, p.evidence_refs_json = h.draft.summary, list(h.draft.evidence_refs)
    p.risk_level, p.reversible, p.rollback_json = (
        h.draft.risk_level.value,
        h.draft.reversible,
        dict(h.draft.rollback),
    )
    p.idempotency_key, p.request_fingerprint = h.draft.idempotency_key, h.draft.request_fingerprint
    p.expires_at, p.continuation_mode = h.draft.expires_at, h.draft.continuation_mode
    p.checkpoint_json, p.checkpoint_checksum = h.draft.checkpoint, h.draft.checkpoint_checksum
    p.checksum = proposal_checksum(
        proposal_content(
            project_id=p.project_id,
            run_id=p.run_id,
            run_segment_id=p.run_segment_id,
            agent_session_id=p.agent_session_id,
            skill_version_id=p.skill_version_id,
            target_binding_id=p.target_binding_id,
            integration_id=None,
            draft=h.draft,
        )
    )
    h.approval.proposal_checksum = p.checksum
    h.execution.provider, h.execution.provider_version = (
        "project-library",
        (LEGACY_DOCUMENT_WRITE_PROVIDER_VERSION if revision == "1"
         else DOCUMENT_WRITE_PROVIDER_VERSION),
    )
    h.execution.idempotency_key, h.execution.request_fingerprint = (
        p.idempotency_key,
        p.request_fingerprint,
    )
    h.rows[ResourceBinding], h.rows[Integration] = h.binding, None
    h.repository._document_library_target = h.target
    h.repository._execution_features = ExecutionFeatures(document_writes=True)
    del h.repository._validate_proposal_row
    h.repository._validate_evidence_refs = AsyncMock()
    h.freeze_skill_snapshot()
    return h


async def test_approved_library_uses_original_artifact_without_an_integration(monkeypatch):
    """実批准行 validator と現在権限/lease を通り、偽 Integration lookup を行わない。"""

    h = await document_proposal(monkeypatch)
    authority = await h.repository.authorize_effect_step(
        h.claimed, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
    )
    assert authority.organization_id == h.actor.organization_id
    assert authority.actor_id == h.actor.id
    assert h.run.permission_snapshot_json["allowed_capabilities"] == [
        "change.propose/v1", "document.read/v1"
    ]
    h.read_artifact.assert_awaited_once_with(
        project_id=h.run.project_id, run_id=h.run.id, artifact_ref="art_fixture"
    )
    assert all(call.args[0] is not Integration for call in h.session.get.call_args_list)
    assert h.locks[:4] == ["Organization", "User", "Project", "ProjectMember"]


@pytest.mark.parametrize("wait_seconds", [0, 40, 100, 180])
async def test_approved_library_claim_preserves_null_integration_and_original_binding(
    monkeypatch, wait_seconds,
):
    """認領と段階認可を続けて通し、ToolCall と claim に偽 Integration を発行しない。"""

    h = await document_proposal(monkeypatch)
    h.proposal.status = "APPROVED"
    h.execution.status, h.execution.attempt_no = "REQUESTED", 0
    h.execution.tool_call_id, h.execution.started_at = None, None
    from skillmind.runs import repository_effects

    now = datetime.now(UTC)

    class Clock:
        """ToolCall flush 待機を進め、取得後の clock が lease に使われるか確認する。"""

        @classmethod
        def now(cls, tz):
            """test が固定した現在時刻を返す。"""
            return now

    async def flush():
        """有効な提案の検証後に、DB 待機時間だけを加える。"""
        nonlocal now
        now += timedelta(seconds=wait_seconds)

    monkeypatch.setattr(repository_effects, "datetime", Clock)
    h.session.flush = flush
    if wait_seconds == 180:
        h.repository._close_effect_before_provider = AsyncMock()

    async def scalars(statement):
        """元 proposal/execution の lock query だけを合成する。"""
        entity = statement.column_descriptions[0]["entity"]
        assert entity in {ChangeProposal, EffectExecution}
        return SimpleNamespace(one=lambda: h.rows[entity])

    h.session.scalars = scalars
    claim = await h.repository.claim_effect_execution(
        h.execution.id,
        worker_id="fixture-worker",
        lease_token=h.claimed.lease_token,
        lease_token_hash_value=lease_token_hash(h.claimed.lease_token),
        lease_seconds=60,
        max_attempts=3,
    )
    if wait_seconds == 180:
        assert claim is None
        closed = h.repository._close_effect_before_provider.call_args.kwargs
        assert closed["code"] == "proposal_expired"
        assert h.execution.status == "REQUESTED"
        return
    assert claim is not None
    assert claim.lease_expires_at == min(now + timedelta(seconds=60), h.proposal.expires_at)
    assert claim.integration_id is None and claim.binding_id == h.binding.id
    assert claim.provider == "project-library" and claim.secret_reference_id is None
    await h.repository.authorize_effect_step(
        claim, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
    )


@pytest.mark.parametrize(
    "mutation", ["namespace", "binding", "snapshot", "artifact", "metadata", "hash", "credential"]
)
async def test_changed_library_or_artifact_cannot_reach_an_authorized_step(monkeypatch, mutation):
    """再承認や現在の同名ファイルで元の保存先/byte/承認 checksum を置き換えない。"""

    h = await document_proposal(monkeypatch)
    if mutation == "namespace":
        h.repository._document_library_target = target()
    elif mutation == "binding":
        h.binding.integration_id = uuid4()
    elif mutation == "snapshot":
        h.run.selected_sources_json[h.draft.resource_key]["document_snapshot"] = {}
    elif mutation == "artifact":
        h.read_artifact.return_value = None
    elif mutation == "metadata":
        h.read_artifact.return_value = ArtifactContent(
            replace(h.artifact.metadata, checksum=f"sha256:{sha256_hex(b'changed')}", size_bytes=7),
            b"changed",
        )
    elif mutation == "hash":
        h.proposal.checksum = "sha256:" + "0" * 64
        h.approval.proposal_checksum = h.proposal.checksum
    else:
        h.claimed = replace(h.claimed, secret_reference_id=uuid4())
    with pytest.raises((ChangeProposalValidationError, EffectLeaseValidationError)):
        await h.repository.authorize_effect_step(
            h.claimed, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
        )


async def test_initial_proposal_uses_the_same_library_and_artifact_checks(monkeypatch):
    """モデル提案の最初の入口でも、確定行と同じ artifact/scope 制約を通す。"""

    h = await document_proposal(monkeypatch)
    blueprint = {
        "effect_intents": [
            {
                "key": h.draft.effect_intent_key,
                "mode": "apply",
                "resource_key": h.draft.resource_key,
                "operation": "CREATE",
                "risk": h.draft.risk_level.value,
            }
        ]
    }
    claimed = SimpleNamespace(
        skill_snapshots_json=({"manifest": {"capability_blueprint": blueprint}},)
    )
    answers = [None, h.binding]
    h.session.scalars = AsyncMock(
        side_effect=lambda _: SimpleNamespace(one_or_none=lambda: answers.pop(0))
    )
    _intent, binding, integration, payload = await h.repository._validate_proposal_draft(
        claimed, run=h.run, draft=h.draft
    )
    assert binding.id == h.binding.id and integration is None
    assert payload["path"] == h.draft.target["locator"] and "object_key" not in payload
    h.read_artifact.assert_awaited_once()


@pytest.mark.parametrize(
    "capability", ["database.write/v1", "repository.write/v1", "issue.update/v1"]
)
async def test_null_integration_is_not_an_escape_for_external_providers(monkeypatch, capability):
    """内部文書庫の例外を他 capability の binding 検証へ流用しない。"""

    h = await document_proposal(monkeypatch)
    with pytest.raises(ChangeProposalValidationError):
        await h.repository._validate_effect_binding(
            run=h.run, binding=h.binding, capability_version=capability
        )


@pytest.mark.parametrize("revision", ["1", "2"])
@pytest.mark.parametrize("allow_legacy_read", [False, True])
async def test_individually_valid_snapshot_and_binding_cannot_mix_revisions(
    monkeypatch, revision, allow_legacy_read,
):
    """両方の checksum が正しくても、元 snapshot と保存行の版が違えば読み替えない。"""

    h = await document_proposal(monkeypatch, revision=revision)
    other = "2" if revision == "1" else "1"
    h.run.selected_sources_json[h.draft.resource_key] = FrozenDocumentLibraryBinding(
        h.run.project_id, h.run.id, h.binding.id, h.draft.resource_key, h.target, other
    ).to_json()
    with pytest.raises(ChangeProposalValidationError):
        await h.repository._validate_effect_binding(
            run=h.run, binding=h.binding, capability_version="document.write/v1",
            allow_legacy_document_read=allow_legacy_read,
        )


async def test_legacy_pending_proposal_cannot_be_approved_or_authorized_for_new_write(monkeypatch):
    """旧束縛を新 Provider に昇格せず、通常の批准/段階入口では原要求を拒否する。"""

    h = await document_proposal(monkeypatch, revision="1")
    original = deepcopy(h.run.selected_sources_json)
    with pytest.raises(ChangeProposalValidationError):
        await h.repository._validate_proposal_row(h.proposal, run=h.run)
    with pytest.raises((ChangeProposalValidationError, EffectLeaseValidationError, ValueError)):
        await h.repository.authorize_effect_step(
            h.claimed, provider_version=DOCUMENT_WRITE_PROVIDER_VERSION
        )
    assert h.run.selected_sources_json == original and h.binding.revision == "1"
