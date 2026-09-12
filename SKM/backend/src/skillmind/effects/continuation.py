"""確定した Effect の原回読を、次 Segment へ渡す有界な公開値として検査する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.redaction import find_sensitive_key

MAX_EFFECT_RESULT_BYTES = 2 * 1024 * 1024
EFFECT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "effect_execution_id", "proposal_ref", "capability_version", "status",
        "after_ref", "after_content_hash", "after", "verification",
    ],
    "properties": {
        "effect_execution_id": {"type": "string", "format": "uuid"},
        "proposal_ref": {"type": "string", "pattern": "^cp_[a-zA-Z0-9_-]+$"},
        "capability_version": {
            "type": "string", "pattern": "^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$",
        },
        "status": {"const": "APPLIED"},
        "before_ref": {"type": "string", "pattern": "^ev_[a-zA-Z0-9_-]+$"},
        "after_ref": {"type": "string", "pattern": "^ev_[a-zA-Z0-9_-]+$"},
        "after_content_hash": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
        "after": {"type": "object"},
        "verification": {"type": "object"},
    },
}
_VALIDATOR = Draft202012Validator(EFFECT_RESULT_SCHEMA, format_checker=FormatChecker())


def validated_effect_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """本文を切り詰めず、原 Evidence hash・JSON 境界・機密 field を検証して分離する。

    この値は原実行の事実であり、新しい書込権や現在の外部状態の証明ではない。
    model の checkpoint 入力には許可せず、finalize の transaction だけで追加する。
    """

    serialized = canonical_json(dict(value))
    if len(serialized.encode("utf-8")) > MAX_EFFECT_RESULT_BYTES:
        raise ValueError("Effect continuation result exceeds its byte limit")
    copied: dict[str, Any] = json.loads(serialized)
    if not _VALIDATOR.is_valid(copied) or find_sensitive_key(copied) is not None:
        raise ValueError("Effect continuation result is invalid")
    if copied["after_content_hash"] != "sha256:" + sha256_hex(canonical_json(copied["after"])):
        raise ValueError("Effect continuation result does not match its Evidence")
    return copied
