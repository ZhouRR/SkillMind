"""汎用 HTTP 変更の原要求、事前観測と回読を承認に固定する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from skillmind.core.hashing import canonical_json
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.integrations.http_resource import validate_request

HTTP_WRITE = "http.write/v1"
HTTP_VERSION = "http-client/v1"


def http_payload(
    operation: str,
    target: Mapping[str, Any],
    changes: tuple[dict[str, Any], ...],
    scope: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """原生 JSON body を保ち、Agent が URL/credential を注入できないようにする。"""
    if (
        len(changes) != 1
        or changes[0].get("path") != "/request"
        or changes[0].get("action") != "SET"
    ):
        raise ValueError("HTTP write requires one SET /request")
    value = changes[0].get("value")
    if not isinstance(value, dict) or set(value) != {"request", "read_back", "checks"}:
        raise ValueError("HTTP write requires request, read_back and checks")
    request, reader, checks = value["request"], value["read_back"], value["checks"]
    if not isinstance(request, dict) or not isinstance(reader, dict):
        raise ValueError("Invalid HTTP requests")
    validate_request(config, scope, request, write=True)
    validate_request(config, scope, reader, write=False)
    if reader.get("method", "GET") != "GET":
        raise ValueError("HTTP read-back requires GET")
    if request.get("method") != operation or target.get("locator") != request.get("path"):
        raise ValueError("HTTP target differs from the approved request")
    if (
        not isinstance(checks, list)
        or not 1 <= len(checks) <= 50
        or any(
            not isinstance(check, dict)
            or set(check) != {"pointer", "equals"}
            or not isinstance(check["pointer"], str)
            or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", check["pointer"]) is None
            for check in checks
        )
    ):
        raise ValueError("HTTP write requires explicit JSON read-back checks")
    if len(canonical_json(value).encode("utf-8")) > 1_048_576:
        raise ValueError("HTTP proposal exceeds the existing effect size limit")
    return dict(value)


def validate_http_proposal(
    draft: ChangeProposalDraft, scope: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """原 observation checksum を固定し、既存の提案/批准に乗せる。"""
    try:
        if draft.capability_version != HTTP_WRITE:
            raise ValueError("Invalid HTTP capability")
        value = http_payload(draft.operation, draft.target, draft.changes, scope, config)
        revision = draft.precondition.get("revision")
        if not isinstance(revision, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", revision) is None:
            raise ValueError("HTTP write requires the original GET response content hash")
        return {**value, "expected_revision": revision}
    except ValueError as error:
        raise ChangeProposalValidationError(str(error)) from None


def http_write_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """実際の二要求だけを scope として記録する。"""
    return {
        "paths": sorted({payload["request"]["path"], payload["read_back"]["path"]}),
        "methods": sorted({payload["request"]["method"], "GET"}),
    }
