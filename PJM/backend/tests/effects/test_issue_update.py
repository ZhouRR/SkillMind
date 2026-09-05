"""issue.update/v1 Proposal の deterministic scope policy を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from projectmind.effects.domain import (
    ChangeProposalDraft,
    ChangeProposalValidationError,
    EffectRiskLevel,
)
from projectmind.effects.issue_update import validate_issue_update_proposal


def _draft(
    *,
    locator: str = "1001",
    changes: tuple[dict[str, Any], ...] = (
        {"path": "/fields/status_id", "action": "SET", "value": "3"},
    ),
) -> ChangeProposalDraft:
    """検証対象の field update Proposal candidate を組み立てる。"""

    return ChangeProposalDraft(
        effect_intent_key="close-ticket",
        resource_key="tickets",
        capability_version="issue.update/v1",
        operation="update_fields",
        target={"locator": locator},
        changes=changes,
        precondition={"revision": "2026-07-19T00:00:00Z"},
        summary="Set status",
        evidence_refs=("evidence:1",),
        risk_level=EffectRiskLevel.LOW,
        reversible=True,
        rollback={},
        verification={},
        continuation_mode="RESUME",
        checkpoint={},
        checkpoint_checksum="sha256:" + ("a" * 64),
        idempotency_key="proposal-001",
        request_fingerprint="sha256:" + ("b" * 64),
        expires_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def test_wildcard_binding_scope_accepts_any_supported_issue_and_field() -> None:
    """明示 wildcard の binding は任意 issue/field の提案を許す(apply は別途承認)。"""

    payload = validate_issue_update_proposal(
        _draft(locator="424242"),
        binding_scope={"issue_ids": ["*"], "field_keys": ["*"]},
    )

    assert payload["issue_id"] == "424242"
    assert payload["requested_scope"] == {
        "issue_ids": ["424242"],
        "field_keys": ["status_id"],
    }


def test_explicit_binding_scope_still_rejects_out_of_scope_updates() -> None:
    """従来の explicit binding は scope 外の issue/field を引き続き拒否する。"""

    with pytest.raises(ChangeProposalValidationError, match="binding scope"):
        validate_issue_update_proposal(
            _draft(locator="9999"),
            binding_scope={"issue_ids": ["1001"], "field_keys": ["status_id"]},
        )
    with pytest.raises(ChangeProposalValidationError, match="binding scope"):
        validate_issue_update_proposal(
            _draft(changes=(
                {"path": "/fields/priority_id", "action": "SET", "value": "2"},
            )),
            binding_scope={"issue_ids": ["1001"], "field_keys": ["status_id"]},
        )


def test_wildcard_scope_does_not_bypass_the_supported_field_pattern() -> None:
    """Wildcard でも platform が対応しない field path は提案自体を拒否する。"""

    with pytest.raises(ChangeProposalValidationError, match="not supported"):
        validate_issue_update_proposal(
            _draft(changes=(
                {"path": "/fields/api_key", "action": "SET", "value": "x"},
            )),
            binding_scope={"issue_ids": ["*"], "field_keys": ["*"]},
        )
