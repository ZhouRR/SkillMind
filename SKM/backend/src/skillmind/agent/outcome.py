"""通用 OutcomeEnvelope と任意 task-specific 結果契約を合成する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.core.hashing import canonical_json, sha256_hex

OUTCOME_ENVELOPE_VERSION = "skillmind.outcome-envelope/v1"
OUTCOME_ENVELOPE_SCHEMA_REF = (
    "https://schemas.skillmind.local/outcomes/envelope/v1.schema.json"
)

# 実行環境は repository 上の contracts path に依存できないため、公開 contract と同じ Schema 本文を
# package 内に保持する。同期 test で差分を拒否し、全 Run が同じ最低境界を使用する。
OUTCOME_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": OUTCOME_ENVELOPE_SCHEMA_REF,
    "title": "Skillmind OutcomeEnvelope v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "outcome_version",
        "summary",
        "status",
        "deliverables",
        "findings",
        "evidence_refs",
        "artifact_refs",
        "open_questions",
        "limitations",
        "confidence",
        "needs_review",
        "change_proposal_refs",
        "effects",
    ],
    "properties": {
        "outcome_version": {"const": OUTCOME_ENVELOPE_VERSION},
        "summary": {"type": "string", "minLength": 1, "maxLength": 10000},
        "status": {"enum": ["COMPLETED", "PARTIAL", "BLOCKED"]},
        "deliverables": {
            "type": "array",
            "maxItems": 200,
            "items": {"$ref": "#/$defs/deliverable"},
        },
        "findings": {
            "type": "array",
            "maxItems": 1000,
            "items": {"$ref": "#/$defs/finding"},
        },
        "evidence_refs": {
            "type": "array",
            "maxItems": 5000,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/evidenceRef"},
        },
        "artifact_refs": {
            "type": "array",
            "maxItems": 1000,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/artifactRef"},
        },
        "open_questions": {
            "type": "array",
            "maxItems": 200,
            "items": {"$ref": "#/$defs/openQuestion"},
        },
        "limitations": {
            "type": "array",
            "maxItems": 200,
            "items": {"type": "string", "minLength": 1, "maxLength": 4000},
        },
        "confidence": {
            "oneOf": [
                {"type": "null"},
                {"type": "number", "minimum": 0, "maximum": 1},
            ]
        },
        "needs_review": {"type": "boolean"},
        "change_proposal_refs": {
            "type": "array",
            "maxItems": 1000,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/changeProposalRef"},
        },
        "effects": {
            "type": "array",
            "maxItems": 1000,
            "items": {"$ref": "#/$defs/effectSummary"},
        },
        "structured_data": {"type": "object"},
    },
    "anyOf": [
        {"properties": {"deliverables": {"minItems": 1}}},
        {"properties": {"findings": {"minItems": 1}}},
        {"properties": {"open_questions": {"minItems": 1}}},
        {"properties": {"limitations": {"minItems": 1}}},
    ],
    "$defs": {
        "key": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_.-]*$",
            "maxLength": 128,
        },
        "evidenceRef": {"type": "string", "pattern": "^ev_[a-zA-Z0-9_-]+$"},
        "artifactRef": {"type": "string", "pattern": "^art_[a-zA-Z0-9_-]+$"},
        "changeProposalRef": {"type": "string", "pattern": "^cp_[a-zA-Z0-9_-]+$"},
        "deliverable": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "kind", "title"],
            "properties": {
                "key": {"$ref": "#/$defs/key"},
                "kind": {
                    "enum": [
                        "report",
                        "structured_data",
                        "patch",
                        "change_proposal",
                        "artifact",
                    ]
                },
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                "content": {"type": "string", "minLength": 1, "maxLength": 200000},
                "artifact_ref": {"$ref": "#/$defs/artifactRef"},
            },
        },
        "finding": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "title", "detail", "evidence_refs"],
            "properties": {
                "key": {"$ref": "#/$defs/key"},
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "detail": {"type": "string", "minLength": 1, "maxLength": 20000},
                "severity": {"enum": ["info", "low", "medium", "high", "critical"]},
                "evidence_refs": {
                    "type": "array",
                    "maxItems": 500,
                    "uniqueItems": True,
                    "items": {"$ref": "#/$defs/evidenceRef"},
                },
            },
        },
        "openQuestion": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "question"],
            "properties": {
                "key": {"$ref": "#/$defs/key"},
                "question": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
        },
        "effectSummary": {
            "type": "object",
            "additionalProperties": False,
            "required": ["proposal_ref", "status", "summary"],
            "properties": {
                "proposal_ref": {"$ref": "#/$defs/changeProposalRef"},
                "status": {
                    "enum": [
                        "PROPOSED",
                        "APPROVED",
                        "REJECTED",
                        "APPLIED",
                        "FAILED",
                        "STALE",
                    ]
                },
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                "before_ref": {"$ref": "#/$defs/evidenceRef"},
                "after_ref": {"$ref": "#/$defs/evidenceRef"},
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class CompiledOutcomeSchema:
    """OutcomeEnvelope と任意の業務 Schema を合成した実行契約。"""

    schema: dict[str, Any]
    checksum: str
    task_schema_checksum: str | None


def compile_outcome_schema(
    task_schema: Mapping[str, Any] | None,
    *,
    task_schema_checksum: str | None,
) -> CompiledOutcomeSchema:
    """通用包絡へ任意 structured_data 契約を埋め、deterministic checksum を返す。"""

    schema = deepcopy(OUTCOME_ENVELOPE_SCHEMA)
    # 動的に業務契約を合成した Schema へ base contract と同じ $id を付けると resolver cache が
    # 別内容を同一視するため、実行 snapshot では識別子を checksum に委ねる。
    schema.pop("$id", None)
    schema.pop("title", None)
    properties = schema["properties"]
    if task_schema is None:
        properties.pop("structured_data")
        effective_task_checksum = None
    else:
        Draft202012Validator.check_schema(task_schema)
        properties["structured_data"] = deepcopy(dict(task_schema))
        schema["required"] = [*schema["required"], "structured_data"]
        effective_task_checksum = task_schema_checksum
    checksum = f"sha256:{sha256_hex(canonical_json(schema))}"
    return CompiledOutcomeSchema(
        schema=schema,
        checksum=checksum,
        task_schema_checksum=effective_task_checksum,
    )
