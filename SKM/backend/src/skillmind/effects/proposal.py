"""Agent の change.propose/v1 input を決定的に検証・正規化する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.redaction import contains_sensitive_content, find_sensitive_key
from skillmind.effects.domain import (
    ChangeProposalDraft,
    ChangeProposalValidationError,
    EffectRiskLevel,
)

CHANGE_PROPOSE_CAPABILITY = "change.propose/v1"
CHANGE_PROPOSE_SDK_NAME = "mcp__skillmind__change_propose_v1"

# Runtime は repository 外の contract path に依存しない。Contract test が公開 JSON Schema と
# この frozen 本文の drift を拒否する。
CHANGE_PROPOSE_REQUEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://schemas.skillmind.local/tools/change.propose/v1/request.schema.json",
    "title": "change.propose/v1 request",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "effect_intent_key",
        "resource_key",
        "capability_version",
        "operation",
        "target",
        "changes",
        "precondition",
        "summary",
        "evidence_refs",
        "risk_level",
        "reversible",
        "rollback",
        "verification",
        "idempotency_key",
        "expires_in_seconds",
        "continuation_mode",
        "checkpoint",
    ],
    "properties": {
        "effect_intent_key": {"$ref": "#/$defs/key"},
        "resource_key": {"$ref": "#/$defs/key"},
        "capability_version": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_.-]*/[vV][0-9][a-zA-Z0-9_.-]*$",
            "maxLength": 128,
        },
        "operation": {"type": "string", "minLength": 1, "maxLength": 128},
        "target": {
            "type": "object",
            "additionalProperties": False,
            "required": ["locator", "display"],
            "properties": {
                "locator": {"type": "string", "minLength": 1, "maxLength": 512},
                "display": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        },
        "changes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "action", "value"],
                "properties": {
                    "path": {
                        "type": "string",
                        "pattern": "^/[a-zA-Z0-9_.~/-]+$",
                        "maxLength": 512,
                    },
                    "action": {"enum": ["SET", "REMOVE", "APPEND"]},
                    "value": {},
                },
            },
        },
        "precondition": {
            "type": "object",
            "additionalProperties": False,
            "required": ["revision"],
            "properties": {
                "revision": {"type": "string", "minLength": 1, "maxLength": 256}
            },
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
        "evidence_refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^ev_[a-zA-Z0-9_-]+$"},
        },
        "risk_level": {"enum": ["LOW", "MEDIUM", "HIGH"]},
        "reversible": {"type": "boolean"},
        "rollback": {
            "type": "object",
            "additionalProperties": False,
            "required": ["description"],
            "properties": {
                "description": {"type": "string", "minLength": 1, "maxLength": 2000}
            },
        },
        "verification": {
            "type": "object",
            "additionalProperties": False,
            "required": ["method", "paths"],
            "properties": {
                "method": {"const": "READ_BACK"},
                "paths": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "pattern": "^/[a-zA-Z0-9_.~/-]+$",
                        "maxLength": 512,
                    },
                },
            },
        },
        "idempotency_key": {
            "type": "string",
            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,127}$",
        },
        "expires_in_seconds": {"type": "integer", "minimum": 300, "maximum": 86400},
        "continuation_mode": {"enum": ["RESUME", "FORK", "REPLACE"]},
        "checkpoint": {"$ref": "#/$defs/checkpoint"},
    },
    "$defs": {
        "key": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_.-]*$",
            "maxLength": 128,
        },
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
}


def parse_change_proposal_request(
    value: Mapping[str, Any], *, now: datetime | None = None
) -> ChangeProposalDraft:
    """Agent の deferred Tool input を Proposal candidate へ検証・正規化する。"""

    payload = dict(value)
    errors = sorted(
        Draft202012Validator(CHANGE_PROPOSE_REQUEST_SCHEMA).iter_errors(payload),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise ChangeProposalValidationError("ChangeProposal request did not match its contract")
    if find_sensitive_key(payload) is not None or _contains_sensitive_text(payload):
        raise ChangeProposalValidationError("ChangeProposal contains sensitive content")
    changes = tuple(dict(item) for item in payload["changes"])
    paths = [str(item["path"]) for item in changes]
    if len(set(paths)) != len(paths):
        raise ChangeProposalValidationError("ChangeProposal changes contain duplicate paths")
    verification = dict(payload["verification"])
    if set(verification["paths"]) != set(paths):
        raise ChangeProposalValidationError(
            "ChangeProposal verification paths must exactly match changed paths"
        )
    checkpoint = dict(payload["checkpoint"])
    current = (now or datetime.now(UTC)).astimezone(UTC)
    fingerprint = sha256_hex(canonical_json(payload))
    return ChangeProposalDraft(
        effect_intent_key=str(payload["effect_intent_key"]),
        resource_key=str(payload["resource_key"]),
        capability_version=str(payload["capability_version"]),
        operation=str(payload["operation"]),
        target=dict(payload["target"]),
        changes=changes,
        precondition=dict(payload["precondition"]),
        summary=str(payload["summary"]),
        evidence_refs=tuple(str(item) for item in payload["evidence_refs"]),
        risk_level=EffectRiskLevel(str(payload["risk_level"])),
        reversible=bool(payload["reversible"]),
        rollback=dict(payload["rollback"]),
        verification=verification,
        idempotency_key=str(payload["idempotency_key"]),
        request_fingerprint=fingerprint,
        expires_at=current + timedelta(seconds=int(payload["expires_in_seconds"])),
        continuation_mode=str(payload["continuation_mode"]),
        checkpoint=checkpoint,
        checkpoint_checksum=f"sha256:{sha256_hex(canonical_json(checkpoint))}",
    )


def proposal_content(
    *,
    project_id: Any,
    run_id: Any,
    run_segment_id: Any,
    agent_session_id: Any,
    skill_version_id: Any,
    target_binding_id: Any,
    integration_id: Any,
    draft: ChangeProposalDraft,
) -> dict[str, Any]:
    """Status と DB identity を除く Proposal version 1 の canonical 本文を返す。"""

    return {
        "project_id": str(project_id),
        "run_id": str(run_id),
        "run_segment_id": str(run_segment_id),
        "agent_session_id": str(agent_session_id),
        "skill_version_id": str(skill_version_id),
        "target_binding_id": str(target_binding_id),
        "integration_id": str(integration_id) if integration_id is not None else None,
        "effect_intent_key": draft.effect_intent_key,
        "resource_key": draft.resource_key,
        "capability_version": draft.capability_version,
        "operation": draft.operation,
        "target": draft.target,
        "changes": list(draft.changes),
        "precondition": draft.precondition,
        "summary": draft.summary,
        "evidence_refs": list(draft.evidence_refs),
        "risk_level": draft.risk_level.value,
        "reversible": draft.reversible,
        "rollback": draft.rollback,
        "verification": draft.verification,
        "idempotency_key": draft.idempotency_key,
        "request_fingerprint": draft.request_fingerprint,
        "expires_at": draft.expires_at.isoformat(),
        "version": 1,
    }


def proposal_checksum(content: Mapping[str, Any]) -> str:
    """Proposal permission-sensitive 本文へ canonical checksum を付ける。"""

    return f"sha256:{sha256_hex(canonical_json(dict(content)))}"


def _contains_sensitive_text(value: Any) -> bool:
    """Proposal の credential assignment/private key marker を再帰検出する。"""

    if isinstance(value, str):
        return contains_sensitive_content(value)
    if isinstance(value, Mapping):
        return any(_contains_sensitive_text(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_sensitive_text(item) for item in value)
    return False
