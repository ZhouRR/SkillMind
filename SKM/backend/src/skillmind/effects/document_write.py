"""文書庫への Artifact 保存提案を、原 path/hash/byte 数と明示 MIME に限定する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from skillmind.artifacts.domain import MAX_ARTIFACT_BYTES
from skillmind.documents.library import DOCUMENT_WRITE_CAPABILITY, document_library_scope
from skillmind.documents.paths import document_effect_storage_key
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.storage.blob import FileStorageError

DOCUMENT_WRITE_PROVIDER_VERSION = "project-library-receipt/v1"


def document_write_scope_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """精確な相対 path だけを scope 摘要へ写す。事前許可は別途常に禁止する。"""

    return {"paths": [payload["path"]]}


def validate_document_write_proposal(
    draft: ChangeProposalDraft,
    *,
    binding_scope: Mapping[str, Any],
) -> dict[str, Any]:
    """モデル本文/接続/任意 object key を受け取らず、保存済み Artifact の参照だけを受理する。"""

    if draft.capability_version != DOCUMENT_WRITE_CAPABILITY:
        raise ChangeProposalValidationError("Document proposal capability is invalid")
    return document_proposal_payload(
        operation=draft.operation, target=draft.target, changes=draft.changes,
        precondition=draft.precondition, verification=draft.verification, scope=binding_scope,
    )


def document_proposal_payload(
    *, operation: str, target: Mapping[str, Any], changes: Sequence[Mapping[str, Any]],
    precondition: Mapping[str, Any], verification: Mapping[str, Any], scope: Mapping[str, Any],
) -> dict[str, Any]:
    """提案と Worker で同じ Artifact/path/CREATE 契約を使用する。"""

    try:
        project_id, _target = document_library_scope(scope)
        if (
            operation != "CREATE"
            or precondition != {"revision": "absent"}
            or verification != {"method": "READ_BACK", "paths": ["/document"]}
            or len(changes) != 1
        ):
            raise ValueError("Invalid document operation")
        change = changes[0]
        if (
            set(change) != {"path", "action", "value"}
            or change["path"] != "/document"
            or change["action"] != "SET"
            or not isinstance(change["value"], Mapping)
        ):
            raise ValueError("Invalid document change")
        value = change["value"]
        if set(value) != {"artifact_ref", "content_hash", "size_bytes", "mime_type"}:
            raise ValueError("Invalid document Artifact reference")
        for key, pattern in (
            ("artifact_ref", r"art_[a-zA-Z0-9_-]{1,60}"),
            ("content_hash", r"sha256:[0-9a-f]{64}"),
        ):
            if not isinstance(value[key], str) or re.fullmatch(pattern, value[key]) is None:
                raise ValueError("Invalid document Artifact identity")
        if (
            type(value["size_bytes"]) is not int
            or not 1 <= value["size_bytes"] <= MAX_ARTIFACT_BYTES
            or value["mime_type"] not in {"text/plain", "text/markdown", "application/json"}
        ):
            raise ValueError("Invalid document content description")
        path = target.get("locator")
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise ValueError("Invalid document target")
        folder, _, name = path.rpartition("/")
        object_key = document_effect_storage_key(project_id, folder, name)
    except (ValueError, TypeError, KeyError, FileStorageError) as error:
        raise ChangeProposalValidationError(
            "Document proposal is outside the Artifact contract"
        ) from error
    return {"path": path, "object_key": object_key, **dict(value)}
