"""Effect の長い外部処理中も現在権限・元批准・lease を再検証する境界を検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skillmind.effects.domain import EffectLeaseValidationError
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from tests.runs.effect_authorization_harness import AuthorizationHarness


async def test_authorization_checks_current_membership_before_run_locks() -> None:
    """現在の user/project を元 Run より先に固定し、批准検証を通す制御を観測する。"""

    h = AuthorizationHarness()
    await h.authorize()
    assert h.locks == [
        "Organization",
        "User",
        "Project",
        "ProjectMember",
        "Run",
        "RunSegment",
        "ChangeProposal",
        "EffectExecution",
    ]
    h.repository._validate_proposal_row.assert_awaited_once()


@pytest.mark.parametrize(
    ("row", "field", "value"),
    [
        ("actor", "status", "DISABLED"),
        ("actor", "organization_id", uuid4()),
        ("project", "status", "ARCHIVED"),
        ("member", "status", "DISABLED"),
        ("run", "status", "CANCELLED"),
        ("segment", "status", "COMPLETED"),
        ("proposal", "status", "REJECTED"),
        ("proposal", "project_id", uuid4()),
        ("proposal", "run_segment_id", uuid4()),
        ("proposal", "operation", "UPDATE"),
        ("proposal", "target_binding_id", uuid4()),
        ("execution", "status", "APPLIED"),
        ("execution", "provider_version", "old/v1"),
        ("execution", "attempt_no", 2),
        ("execution", "lease_token_hash", "other"),
        ("execution", "approval_id", uuid4()),
        ("execution", "request_fingerprint", "other"),
        ("approval", "decision", "REJECTED"),
        ("approval", "source", "PREAUTHORIZATION"),
        ("approval", "proposal_version", 2),
        ("approval", "proposal_checksum", "b" * 71),
        ("integration", "revision", 2),
        ("integration", "secret_reference_id", uuid4()),
    ],
)
async def test_changed_authority_or_claimed_snapshot_is_rejected(row, field, value) -> None:
    """権限撤回と frozen identity/version の変更を、外部 I/O へ進める前に拒否する。"""

    h = AuthorizationHarness()
    setattr(getattr(h, row), field, value)
    with pytest.raises((EffectLeaseValidationError, ProjectArchivedError, ProjectNotFoundError)):
        await h.authorize()


async def test_cancellation_and_capability_removal_are_rejected() -> None:
    """待機 Run でも取消要求と凍結権限上限の不足を拒否する。"""

    h = AuthorizationHarness()
    h.repository.is_cancellation_requested.return_value = True
    with pytest.raises(EffectLeaseValidationError, match="cancelled"):
        await h.authorize()
    h.repository.is_cancellation_requested.return_value = False
    h.run.permission_snapshot_json["allowed_capabilities"] = []
    with pytest.raises(EffectLeaseValidationError):
        await h.authorize()


@pytest.mark.parametrize("row", ["proposal", "execution"])
async def test_expiry_after_database_validation_wait_is_rechecked(row) -> None:
    """認可の途中待機で寿命を使い切った要求を、最初の now だけで通過させない。"""

    h = AuthorizationHarness()

    async def expire(*args, **kwargs):
        """checksum/binding 検証の待機中に原寿命が尽きた状況を再現する。"""
        field = "expires_at" if row == "proposal" else "lease_expires_at"
        setattr(getattr(h, row), field, datetime.now(UTC) - timedelta(seconds=1))

    h.repository._validate_proposal_row.side_effect = expire
    with pytest.raises(EffectLeaseValidationError, match="expired"):
        await h.authorize()


async def test_json_boolean_is_not_interchangeable_with_approved_integer() -> None:
    """Python の True == 1 を利用した承認 JSON の差替えを canonical 値で拒否する。"""

    h = AuthorizationHarness()
    h.proposal.preview_json["changes"][0]["value"]["values"]["status"] = 1
    h.claimed.changes[0]["value"]["values"]["status"] = True
    with pytest.raises(EffectLeaseValidationError, match="snapshot"):
        await h.authorize()


async def test_claimed_connection_and_scope_cannot_be_changed() -> None:
    """現在 Integration と元 claim が違う場合は宛先/許可列を拡張できない。"""

    h = AuthorizationHarness()
    h.claimed = replace(h.claimed, integration_config={"host": "other.example.test"})
    with pytest.raises(EffectLeaseValidationError, match="snapshot"):
        await h.authorize()
    h = AuthorizationHarness()
    h.claimed.integration_scope["write_columns"].append("example.records.other")
    with pytest.raises(EffectLeaseValidationError, match="snapshot"):
        await h.authorize()
