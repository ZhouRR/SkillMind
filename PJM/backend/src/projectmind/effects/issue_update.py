"""issue.update/v1 Proposal と Integration scope の deterministic policy を定義する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from projectmind.core.hashing import canonical_json
from projectmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from projectmind.integrations.domain import scope_is_subset

ISSUE_UPDATE_CAPABILITY = "issue.update/v1"
ISSUE_UPDATE_PROVIDER_VERSION = "redmine-cas/v1"

_ISSUE_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,17}$")
_FIELD_KEY_PATTERN = re.compile(
    r"^(status_id|priority_id|assigned_to_id|category_id|fixed_version_id|done_ratio|"
    r"subject|description|start_date|due_date|estimated_hours|custom_field\.[1-9][0-9]{0,9})$"
)


def validate_issue_update_proposal(
    draft: ChangeProposalDraft, *, binding_scope: Mapping[str, Any]
) -> dict[str, Any]:
    """Redmine field update を explicit issue/field scope と idempotent SET に限定する。"""

    if draft.capability_version != ISSUE_UPDATE_CAPABILITY:
        raise ChangeProposalValidationError("Effect capability is not issue.update/v1")
    locator = draft.target.get("locator")
    if not isinstance(locator, str) or _ISSUE_ID_PATTERN.fullmatch(locator) is None:
        raise ChangeProposalValidationError("Issue target locator must be a numeric issue ID")
    fields: dict[str, Any] = {}
    for change in draft.changes:
        path = change.get("path")
        action = change.get("action")
        if not isinstance(path, str) or not path.startswith("/fields/"):
            raise ChangeProposalValidationError("Issue update path must stay under /fields")
        field_key = path.removeprefix("/fields/")
        if _FIELD_KEY_PATTERN.fullmatch(field_key) is None:
            raise ChangeProposalValidationError("Issue update field is not supported")
        if action != "SET":
            # Append (notes/comment) は Redmine 側で厳密な replay 判定ができないため第一版では禁止。
            raise ChangeProposalValidationError("issue.update/v1 only permits idempotent SET")
        value = change.get("value")
        if isinstance(value, dict) or len(canonical_json(value)) > 20_000:
            raise ChangeProposalValidationError("Issue update value is invalid or too large")
        fields[field_key] = value
    requested_scope = {"issue_ids": [locator], "field_keys": sorted(fields)}
    allowed_scope = {
        "issue_ids": list(binding_scope.get("issue_ids", [])),
        "field_keys": list(binding_scope.get("field_keys", [])),
    }
    if not scope_is_subset(requested=requested_scope, allowed=allowed_scope):
        raise ChangeProposalValidationError("Issue update exceeds the frozen binding scope")
    return {
        "issue_id": locator,
        "fields": fields,
        "expected_revision": draft.precondition["revision"],
        "requested_scope": requested_scope,
    }


def issue_update_scope_from_payload(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """Provider payload から preauthorization 照合用の最小 scope を返す。"""

    issue_id = payload.get("issue_id")
    fields = payload.get("fields")
    if not isinstance(issue_id, str) or not isinstance(fields, Mapping):
        raise ChangeProposalValidationError("Issue update payload is invalid")
    return {"issue_ids": [issue_id], "field_keys": sorted(str(key) for key in fields)}
