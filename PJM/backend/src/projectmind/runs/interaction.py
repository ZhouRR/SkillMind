"""構造化 UserInteraction と回答 payload の決定的 validation を提供する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.core.redaction import contains_sensitive_content, find_sensitive_key
from projectmind.runs.domain import (
    InteractionResponseInvalidError,
    SessionContinuationMode,
    UserInteractionType,
)

INTERACTION_REQUEST_CAPABILITY = "interaction.request/v1"
INTERACTION_REQUEST_SDK_NAME = "mcp__projectmind__interaction_request_v1"
ORDINARY_INTERACTION_TYPES = (
    UserInteractionType.CLARIFICATION,
    UserInteractionType.CHOICE,
    UserInteractionType.REVIEW,
)

# Runtime は repository 外の contract file に依存しない。公開 Tool contract と完全一致する本文を
# package に保持し、contract test が drift を拒否する。
INTERACTION_REQUEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://schemas.projectmind.local/tools/interaction.request/v1/request.schema.json",
    "title": "interaction.request/v1 request",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "interaction_type",
        "prompt",
        "rationale",
        "impact",
        "options",
        "allow_multiple",
        "required",
        "expires_in_seconds",
        "continuation_mode",
        "checkpoint",
    ],
    "properties": {
        "interaction_type": {
            "enum": [item.value for item in ORDINARY_INTERACTION_TYPES]
        },
        "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 2000},
        "impact": {"type": "string", "minLength": 1, "maxLength": 2000},
        "options": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "label", "description", "recommended"],
                "properties": {
                    "key": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_.-]*$",
                        "maxLength": 128,
                        "description": "Stable option identity, unique within this question.",
                    },
                    "label": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "recommended": {"type": "boolean"},
                },
            },
        },
        "allow_multiple": {"type": "boolean"},
        "required": {"type": "boolean"},
        "expires_in_seconds": {"type": "integer", "minimum": 300, "maximum": 604800},
        "continuation_mode": {"enum": ["RESUME", "FORK", "REPLACE"]},
        "checkpoint": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary",
                "confirmed_facts",
                "evidence_refs",
                "artifact_refs",
                "change_proposal_refs",
            ],
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                "confirmed_facts": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "evidence_refs": {
                    "type": "array",
                    "maxItems": 200,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^ev_[a-zA-Z0-9_-]+$"},
                },
                "artifact_refs": {
                    "type": "array",
                    "maxItems": 100,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^art_[a-zA-Z0-9_-]+$"},
                },
                "change_proposal_refs": {
                    "type": "array",
                    "maxItems": 100,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^cp_[a-zA-Z0-9_-]+$"},
                },
            },
        },
    },
    "allOf": [
        {
            "if": {
                "properties": {"interaction_type": {"const": "CHOICE"}},
                "required": ["interaction_type"],
            },
            "then": {"properties": {"options": {"minItems": 2}}},
            "else": {"properties": {"allow_multiple": {"const": False}}},
        }
    ],
}


@dataclass(frozen=True, slots=True)
class InteractionRequestDraft:
    """Schema 検証済みの公開 interaction と checkpoint。"""

    interaction_type: UserInteractionType
    prompt: dict[str, Any]
    options: tuple[dict[str, Any], ...]
    required: bool
    expires_at: datetime
    continuation_mode: SessionContinuationMode
    checkpoint: dict[str, Any]
    checkpoint_checksum: str


def require_ordinary_interaction(interaction_type: UserInteractionType) -> None:
    """Proposal の承認を一般質問へ流さず、parser を通らない内部呼出しにも同じ門禁を課す。"""

    if interaction_type not in ORDINARY_INTERACTION_TYPES:
        raise InteractionResponseInvalidError("Effect approval requires the approval endpoint")


def validate_interaction_option_keys(options: tuple[Mapping[str, Any], ...]) -> None:
    """新規質問の key を一意にし、回答から異なる選択肢を区別できるようにする。"""

    keys = [option.get("key") for option in options]
    # JSON Schema の uniqueItems では label が異なる同一 key を拒否できない。
    if not all(isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
        raise InteractionResponseInvalidError("Interaction option keys must be unique strings")


def validate_interaction_version(version: object) -> None:
    """問題の原版を bool/小数/文字列から暗黙変換せず、正整数として扱う。"""

    if type(version) is not int or version < 1:
        raise InteractionResponseInvalidError("Interaction version must be a positive integer")


def parse_interaction_request(
    value: Mapping[str, Any], *, now: datetime | None = None
) -> InteractionRequestDraft:
    """Agent の deferred Tool input を公開可能な Interaction へ検証・正規化する。"""

    payload = dict(value)
    errors = sorted(
        Draft202012Validator(INTERACTION_REQUEST_SCHEMA).iter_errors(payload),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise InteractionResponseInvalidError("Interaction request did not match its contract")
    if find_sensitive_key(payload) is not None or _contains_sensitive_text(payload):
        raise InteractionResponseInvalidError("Interaction request contains sensitive content")
    options = tuple(dict(item) for item in payload["options"])
    validate_interaction_option_keys(options)
    checkpoint = dict(payload["checkpoint"])
    current = (now or datetime.now(UTC)).astimezone(UTC)
    return InteractionRequestDraft(
        interaction_type=UserInteractionType(str(payload["interaction_type"])),
        prompt={
            "prompt": str(payload["prompt"]),
            "rationale": str(payload["rationale"]),
            "impact": str(payload["impact"]),
            "allow_multiple": bool(payload["allow_multiple"]),
        },
        options=options,
        required=bool(payload["required"]),
        expires_at=current + timedelta(seconds=int(payload["expires_in_seconds"])),
        continuation_mode=SessionContinuationMode(str(payload["continuation_mode"])),
        checkpoint=checkpoint,
        checkpoint_checksum=f"sha256:{sha256_hex(canonical_json(checkpoint))}",
    )


def validate_interaction_answer(response: Mapping[str, Any]) -> dict[str, Any]:
    """HTTP と内部応答の共通形状を検査し、本文や選択順を変えずに複製する。"""

    payload = dict(response)
    if set(payload) - {"text", "selected_option_keys"}:
        raise InteractionResponseInvalidError("Interaction response contains unknown fields")
    text = payload.get("text")
    selected = payload.get("selected_option_keys")
    if "text" in payload and (not isinstance(text, str) or not 1 <= len(text) <= 10_000):
        raise InteractionResponseInvalidError("Interaction response text is invalid")
    if "selected_option_keys" in payload and (
        not isinstance(selected, list)
        or not selected
        or len(selected) > 20
        or not all(
            isinstance(item, str) and len(item) <= 128
            and re.fullmatch(r"[a-z][a-z0-9_.-]*", item) is not None
            for item in selected
        )
        or len(set(selected)) != len(selected)
    ):
        raise InteractionResponseInvalidError("Interaction selected options are invalid")
    if text is None and selected is None:
        raise InteractionResponseInvalidError("Interaction response is empty")
    if find_sensitive_key(payload) is not None or _contains_sensitive_text(payload):
        raise InteractionResponseInvalidError("Interaction response contains sensitive content")
    normalized: dict[str, Any] = {}
    if isinstance(text, str):
        normalized["text"] = text
    if isinstance(selected, list):
        normalized["selected_option_keys"] = list(selected)
    return normalized


def validate_interaction_response(
    *,
    interaction_type: UserInteractionType,
    prompt: Mapping[str, Any],
    options: tuple[Mapping[str, Any], ...],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Interaction 種別と選択肢に対して user response を正規化する。"""

    require_ordinary_interaction(interaction_type)
    normalized = validate_interaction_answer(response)
    selected = normalized.get("selected_option_keys")
    if interaction_type is UserInteractionType.CHOICE:
        if not isinstance(selected, list):
            raise InteractionResponseInvalidError("Choice response requires selected options")
        known = {str(item.get("key")) for item in options}
        if any(item not in known for item in selected):
            raise InteractionResponseInvalidError("Choice response selected an unknown option")
        allow_multiple = bool(prompt.get("allow_multiple", False))
        if not allow_multiple and len(selected) != 1:
            raise InteractionResponseInvalidError("Choice response accepts exactly one option")
    elif selected is not None:
        raise InteractionResponseInvalidError("This interaction does not accept option keys")
    return normalized


def interaction_response_hash(
    *, interaction_id: Any, interaction_version: int, response: Mapping[str, Any]
) -> str:
    """Interaction response の idempotency fingerprint を正規化 JSON から作る。"""

    return sha256_hex(
        canonical_json(
            {
                "interaction_id": str(interaction_id),
                "interaction_version": interaction_version,
                "response": dict(response),
            }
        )
    )


def _contains_sensitive_text(value: Any) -> bool:
    """公開 interaction 内の credential assignment/private key marker を再帰検出する。"""

    if isinstance(value, str):
        return contains_sensitive_content(value)
    if isinstance(value, Mapping):
        return any(_contains_sensitive_text(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_sensitive_text(item) for item in value)
    return False
