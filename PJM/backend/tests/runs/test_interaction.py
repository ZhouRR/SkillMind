"""UserInteraction request/response の純粋な境界を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from projectmind.runs.domain import (
    InteractionResponseInvalidError,
    SessionContinuationMode,
    UserInteractionType,
)
from projectmind.runs.interaction import (
    interaction_response_hash,
    parse_interaction_request,
    validate_interaction_response,
)


def _request(**overrides: object) -> dict[str, object]:
    """Schema 有効な REVIEW request をテストごとに上書きできる形で返す。"""

    value: dict[str, object] = {
        "interaction_type": "REVIEW",
        "prompt": "Please review the finding.",
        "rationale": "The final severity depends on the project context.",
        "impact": "The response may change the final recommendation.",
        "options": [],
        "allow_multiple": False,
        "required": True,
        "expires_in_seconds": 3600,
        "continuation_mode": "FORK",
        "checkpoint": {
            "summary": "Analysis is ready for review.",
            "confirmed_facts": ["The affected path exists in the snapshot."],
            "evidence_refs": ["ev_fixture_001"],
            "artifact_refs": [],
            "change_proposal_refs": [],
        },
    }
    value.update(overrides)
    return value


def test_parse_interaction_request_freezes_public_checkpoint() -> None:
    """公開質問、期限、continuation と checkpoint checksum が一度に固定される。"""

    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    parsed = parse_interaction_request(_request(), now=now)

    assert parsed.interaction_type is UserInteractionType.REVIEW
    assert parsed.continuation_mode is SessionContinuationMode.FORK
    assert parsed.prompt["allow_multiple"] is False
    assert parsed.expires_at == now + timedelta(hours=1)
    assert parsed.checkpoint["evidence_refs"] == ["ev_fixture_001"]
    assert parsed.checkpoint_checksum.startswith("sha256:")


def test_parse_interaction_request_rejects_sensitive_content() -> None:
    """User-facing prompt へ credential らしい内容を持ち出さない。"""

    with pytest.raises(InteractionResponseInvalidError, match="sensitive"):
        parse_interaction_request(_request(prompt="api_key=secret-value"))


def test_choice_response_obeys_known_options_and_multiplicity() -> None:
    """CHOICE は提示済み key だけを受理し、単一選択を迂回できない。"""

    options = (
        {"key": "accept", "label": "Accept", "description": "Keep the finding."},
        {"key": "revise", "label": "Revise", "description": "Change the finding."},
    )
    normalized = validate_interaction_response(
        interaction_type=UserInteractionType.CHOICE,
        prompt={"allow_multiple": False},
        options=options,
        response={"selected_option_keys": ["accept"]},
    )
    assert normalized == {"selected_option_keys": ["accept"]}

    with pytest.raises(InteractionResponseInvalidError, match="unknown option"):
        validate_interaction_response(
            interaction_type=UserInteractionType.CHOICE,
            prompt={"allow_multiple": False},
            options=options,
            response={"selected_option_keys": ["missing"]},
        )
    with pytest.raises(InteractionResponseInvalidError, match="exactly one"):
        validate_interaction_response(
            interaction_type=UserInteractionType.CHOICE,
            prompt={"allow_multiple": False},
            options=options,
            response={"selected_option_keys": ["accept", "revise"]},
        )


def test_effect_approval_cannot_use_generic_response_endpoint() -> None:
    """Effect approval を Proposal version 検証のない一般回答へ流さない。"""

    with pytest.raises(InteractionResponseInvalidError, match="approval endpoint"):
        validate_interaction_response(
            interaction_type=UserInteractionType.EFFECT_APPROVAL,
            prompt={"allow_multiple": False},
            options=(),
            response={"text": "approve"},
        )


def test_interaction_response_hash_binds_version_and_payload() -> None:
    """Idempotency fingerprint が key 順に依存せず version 差を区別する。"""

    interaction_id = UUID("00000000-0000-4000-8000-000000000123")
    first = interaction_response_hash(
        interaction_id=interaction_id,
        interaction_version=1,
        response={"text": "Continue", "selected_option_keys": ["accept"]},
    )
    reordered = interaction_response_hash(
        interaction_id=interaction_id,
        interaction_version=1,
        response={"selected_option_keys": ["accept"], "text": "Continue"},
    )
    changed = interaction_response_hash(
        interaction_id=interaction_id,
        interaction_version=2,
        response={"text": "Continue", "selected_option_keys": ["accept"]},
    )

    assert first == reordered
    assert first != changed
