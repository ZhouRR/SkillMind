"""核対対象の再構築を実提案 validator で検証する。SQL/Artifact storage は double。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from skillmind.core.hashing import sha256_hex
from skillmind.effects.database_write import build_database_write, database_proposal_payload
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.proposal import proposal_checksum, proposal_content
from skillmind.effects.release import ExecutionFeatures
from tests.runs.effect_authorization_harness import AuthorizationHarness
from tests.runs.test_document_proposals import document_proposal


async def stopped_document(monkeypatch, *, revision="2"):
    """停止済み Run と失効 lease を持つ、変更しない原提案/承認を作る。"""
    h = await document_proposal(monkeypatch, revision=revision)
    h.run.status, h.execution.status = "CANCELLED", "FAILED"
    h.execution.error_json = {"code": "effect_result_unknown", "retryable": False}
    h.execution.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    h.execution.lease_token_hash = None
    h.repository._execution_features = ExecutionFeatures()
    return h


async def load(h, **overrides):
    """Project/Run/Effect だけを渡し、書込 claim や新 ID を渡さない。"""
    args = dict(project_id=h.run.project_id, run_id=h.run.id, effect_execution_id=h.execution.id)
    return await h.repository.load_effect_reconciliation_target(**{**args, **overrides})


async def test_cancelled_original_can_be_reconstructed_with_all_writes_disabled(monkeypatch):
    """現在の参照認可とは別に、原 lease/取消/配備 write gate を照会権として使わない。"""
    h = await stopped_document(monkeypatch)
    original = deepcopy(vars(h.execution))
    target = await load(h)
    assert target.command.effect_id == h.execution.id
    assert target.command.project_id == h.run.project_id and target.command.run_id == h.run.id
    assert target.command.content == h.artifact.content
    assert target.command.namespace == h.target.namespace
    assert target.provider == "project-library" and target.secret_reference_id is None
    assert target.proposal_checksum == h.approval.proposal_checksum
    assert vars(h.execution) == original
    assert h.run.status == "CANCELLED"
    assert not hasattr(target, "lease_token")


async def test_expired_exact_approval_does_not_require_a_new_write_approval_for_read(monkeypatch):
    """古い期限を原 checksum に含む正規 record を読み、新批准や新 Effect を作らない。"""
    h = await stopped_document(monkeypatch)
    p = h.proposal
    h.draft = replace(h.draft, expires_at=datetime.now(UTC) - timedelta(days=1))
    p.expires_at = h.draft.expires_at
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
    target = await load(h)
    assert target.proposal_checksum == h.approval.proposal_checksum
    assert target.command.effect_id == h.execution.id


async def test_database_target_rebuilds_same_command_from_shared_validated_payload():
    """共通 validator の返却値を再利用し、元 table/key/値/identity を書換えず照会へ渡す。"""
    h = AuthorizationHarness()
    c = h.claimed
    h.execution.error_json = {"code": "effect_result_unknown", "retryable": False}
    h.execution.status = "FAILED"
    h.run.status = "FAILED"
    h.repository._execution_features = ExecutionFeatures()
    payload = database_proposal_payload(
        operation=c.operation,
        target=c.target,
        changes=c.changes,
        precondition=c.precondition,
        verification=c.verification,
        scope=c.integration_scope,
    )
    h.repository._validate_proposal_row.return_value = payload
    target = await load(h)
    expected = build_database_write(
        effect_id=c.effect_execution_id,
        project_id=c.project_id,
        run_id=c.run_id,
        integration_id=c.integration_id,
        scope=c.integration_scope,
        **payload,
    )
    assert target.command == expected
    assert target.binding_id == c.binding_id
    assert target.secret_reference_id == c.secret_reference_id
    h.repository._validate_proposal_row.assert_awaited_once_with(h.proposal, run=h.run)


@pytest.mark.parametrize(
    "mutation",
    [
        "effect_run",
        "proposal_run",
        "proposal_project",
        "approval",
        "fingerprint",
        "provider",
        "target",
        "artifact",
        "namespace",
        "not_claimed",
        "not_unknown",
    ],
)
async def test_read_cannot_switch_original_effect_request_or_bytes(monkeypatch, mutation):
    """失効後の照会でも原批准/帰属/保存先/byte の差替えを許可しない。"""
    h = await stopped_document(monkeypatch)
    if mutation == "effect_run":
        h.execution.run_id = uuid4()
    elif mutation == "proposal_run":
        h.proposal.run_id = uuid4()
    elif mutation == "proposal_project":
        h.proposal.project_id = uuid4()
    elif mutation == "approval":
        h.approval.proposal_checksum = "sha256:" + "0" * 64
    elif mutation == "fingerprint":
        h.execution.request_fingerprint = "changed"
    elif mutation == "provider":
        h.execution.provider_version = "different/v1"
    elif mutation == "target":
        h.proposal.target_json["locator"] = "other.md"
    elif mutation == "artifact":
        h.read_artifact.return_value = replace(
            h.artifact,
            content=b"changed",
            metadata=replace(
                h.artifact.metadata, size_bytes=7, checksum=f"sha256:{sha256_hex(b'changed')}"
            ),
        )
    elif mutation == "namespace":
        h.repository._document_library_target = replace(h.target, bucket="other-bucket")
    elif mutation == "not_claimed":
        h.execution.attempt_no = 0
    else:
        h.execution.error_json = None
    with pytest.raises((ValueError, LookupError, ChangeProposalValidationError)):
        await load(h)


async def test_legacy_reconciliation_reconstructs_original_scope_key_and_metadata(monkeypatch):
    """v1 原批准は新 key/checksum に置換せず、閉じた write gate のまま只読対象にする。"""

    h = await stopped_document(monkeypatch, revision="1")
    original = deepcopy(h.run.selected_sources_json)
    target = await load(h)
    command = target.command
    assert command.protocol_version == 1
    assert target.provider_version == "project-library-receipt/v1"
    assert command.logical_path == h.draft.target["locator"]
    assert command.object_key == (
        f"projects/{h.run.project_id}/documents/effects/{h.draft.target['locator']}"
    )
    assert command.origin_metadata["skm-protocol"] == "artifact-object-create/v1"
    assert command.origin_metadata["skm-request-checksum"] == command.request_checksum
    assert h.run.selected_sources_json == original and h.binding.revision == "1"


@pytest.mark.parametrize("revision,provider", [
    ("1", "project-library-receipt/v2"), ("2", "project-library-receipt/v1"),
])
async def test_reconciliation_cannot_change_the_original_object_protocol(
    monkeypatch, revision, provider,
):
    """Provider 版だけを差し替えて旧承認を新 key 空間へ向ける操作も拒否する。"""

    h = await stopped_document(monkeypatch, revision=revision)
    h.execution.provider_version = provider
    with pytest.raises(ValueError, match="protocol"):
        await load(h)
