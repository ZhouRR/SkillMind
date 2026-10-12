"""HTTP の原要求を承認に固定し、不要な事前 GET と回読を要求しない。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skillmind.core.hashing import canonical_json
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.integrations.http_resource import validate_request

HTTP_WRITE = "http.write/v1"
HTTP_VERSION = "http-client/v2"


def http_payload(
    operation: str, target: Mapping[str, Any], changes: tuple[dict[str, Any], ...],
    scope: Mapping[str, Any], config: Mapping[str, Any],
) -> dict[str, Any]:
    """一つの request を検査し、Agent の business body を変更しない。"""
    if (len(changes) != 1 or changes[0].get("path") != "/request"
        or changes[0].get("action") != "SET"):
        raise ValueError("HTTP write requires one SET /request")
    value = changes[0].get("value")
    if not isinstance(value, dict) or set(value) != {"request"}:
        raise ValueError("HTTP write requires only request")
    request = value["request"]
    if not isinstance(request, dict):
        raise ValueError("Invalid HTTP request")
    validate_request(config, scope, request, write=True)
    if (request.get("method") != operation
        or target.get("locator") != request.get("url", request.get("path"))):
        raise ValueError("HTTP target differs from the approved request")
    if len(canonical_json(value).encode("utf-8")) > 1_048_576:
        raise ValueError("HTTP proposal exceeds the existing effect size limit")
    return dict(value)


def validate_http_proposal(
    draft: ChangeProposalDraft, scope: Mapping[str, Any], config: Mapping[str, Any],
) -> dict[str, Any]:
    """HTTP の確認は原 response に限定し、外部 revision や GET 根拠を作らせない。"""
    try:
        if (draft.capability_version != HTTP_WRITE or draft.precondition
            or draft.verification.get("method") != "HTTP_RESPONSE"):
            raise ValueError("HTTP writes use response verification without a GET precondition")
        return http_payload(draft.operation, draft.target, draft.changes, scope, config)
    except ValueError as error:
        raise ChangeProposalValidationError(str(error)) from None


def http_write_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """HTTP は Project 事前許可を持たず、登録根と原 request を直接検査する。"""
    return {}
