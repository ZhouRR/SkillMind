"""UserInteraction request/response の純粋な境界を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from skillmind.runs.domain import (
    InteractionResponseInvalidError,
    SessionContinuationMode,
    UserInteractionType,
)
from skillmind.runs.interaction import (
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


@pytest.mark.parametrize("interaction_type", ["CLARIFICATION", "CHOICE", "REVIEW"])
def test_question_rejects_duplicate_option_identity(interaction_type: str) -> None:
    """異なる label でも同じ key は回答から識別できず、新しい質問として保存しない。"""

    options = [
        {"key": "same", "label": "First", "description": "First scope", "recommended": True},
        {"key": "same", "label": "Second", "description": "Other scope", "recommended": False},
    ]
    with pytest.raises(InteractionResponseInvalidError, match="unique"):
        parse_interaction_request(_request(interaction_type=interaction_type, options=options))


def test_question_preserves_distinct_option_identity_and_order() -> None:
    """新規質問の識別だけを検証し、推薦・選択順・既存 hash 規則を正規化しない。"""

    options = [
        {"key": key, "label": key, "description": "Public scope", "recommended": False}
        for key in ("second", "first")
    ]
    parsed = parse_interaction_request(_request(interaction_type="CHOICE", options=options))
    assert list(parsed.options) == options


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
    assert first == "9579bd2b81483c31a135342964494f5df06b5b386c9d72a296cc65255b8997ec"


@pytest.mark.parametrize("interaction_type", ["EFFECT_APPROVAL", "UNKNOWN"])
def test_ordinary_request_rejects_approval_and_unknown_types(interaction_type: str) -> None:
    """Proposal の無い批准待ちを公開 Tool/parser のどちらからも作成しない。"""

    with pytest.raises(InteractionResponseInvalidError, match="contract"):
        parse_interaction_request(_request(interaction_type=interaction_type))


@pytest.mark.parametrize("interaction_type", ["CLARIFICATION", "REVIEW"])
def test_optional_ordinary_questions_still_require_explicit_answers(interaction_type: str) -> None:
    """required=false を skip や暗黙の推薦回答として扱わない。"""

    request = parse_interaction_request(_request(interaction_type=interaction_type, required=False))
    assert request.required is False
    with pytest.raises(InteractionResponseInvalidError, match="empty"):
        validate_interaction_response(
            interaction_type=request.interaction_type, prompt=request.prompt,
            options=request.options, response={},
        )
    assert validate_interaction_response(
        interaction_type=request.interaction_type, prompt=request.prompt,
        options=request.options, response={"text": " "},
    ) == {"text": " "}
