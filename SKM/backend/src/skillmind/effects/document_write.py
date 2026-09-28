"""文書庫への Artifact 保存提案を、原 path/hash/byte 数と明示 MIME に限定する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from skillmind.artifacts.domain import MAX_ARTIFACT_BYTES
from skillmind.documents.file_state import FILE_OPERATIONS
from skillmind.documents.library import (
    DOCUMENT_WRITE_CAPABILITY,
    document_library_revision,
    document_library_scope,
)
from skillmind.documents.paths import document_effect_storage_key, validate_document_path
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.storage.blob import FileStorageError

DOCUMENT_WRITE_PROVIDER_VERSION = "project-library-receipt/v2"
LEGACY_DOCUMENT_WRITE_PROVIDER_VERSION = "project-library-receipt/v1"


def document_write_scope_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """精確な相対 path だけを scope 摘要へ写す。事前許可は別途常に禁止する。"""

    return {
        "paths": sorted(
            {payload["path"], *([payload["destination"]] if payload.get("destination") else [])}
        )
    }


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
    *,
    operation: str,
    target: Mapping[str, Any],
    changes: Sequence[Mapping[str, Any]],
    precondition: Mapping[str, Any],
    verification: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """提案と Worker で同じ Artifact/path/版付き変更契約を使用する。"""

    try:
        project_id, _target = document_library_scope(scope)
        if (
            operation not in {"CREATE", "UPDATE", *FILE_OPERATIONS}
            or (operation != "CREATE" and document_library_revision(scope) != "2")
            or set(precondition) != {"revision"}
            or not isinstance(precondition["revision"], str)
            or (
                precondition["revision"] != "absent"
                if operation in {"CREATE", "CREATE_FOLDER"}
                else re.fullmatch(r"sha256:[0-9a-f]{64}", precondition["revision"]) is None
            )
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
        if operation in FILE_OPERATIONS:
            return _management_payload(project_id, operation, target, value, precondition)
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
        if validate_document_path(project_id=project_id, folder=folder, name=name) != (
            folder,
            name,
        ):
            raise ValueError("Document target is not canonical")
    except (ValueError, TypeError, KeyError, FileStorageError) as error:
        raise ChangeProposalValidationError(
            "Document proposal is outside the Artifact contract"
        ) from error
    payload = {"path": path, **dict(value)}
    if operation == "UPDATE":
        payload["expected_revision"] = precondition["revision"]
    # v1 回执の再構築だけが旧 key を必要とする。v2 の物理 key は批准後に確定する
    # Effect ID と実 Artifact byte に束縛し、提案時には生成しない。
    if document_library_revision(scope) == "1":
        payload["object_key"] = document_effect_storage_key(project_id, folder, name)
    return payload


def _management_payload(
    project_id: UUID, operation: str, target: Mapping[str, Any],
    value: Mapping[str, Any], precondition: Mapping[str, Any],
) -> dict[str, Any]:
    """目录操作は対象 path と必要な移動先だけ。任意 metadata や正文を通さない。"""
    path = target.get("locator")
    folder, _, name = str(path).rpartition("/")
    if not isinstance(path, str) or validate_document_path(
        project_id=project_id, folder=folder, name=name
    ) != (folder, name):
        raise ValueError("Invalid management path")
    moving = operation in {"MOVE", "MOVE_FOLDER"}
    if set(value) != (
        {"destination"} if moving else {"document_id"} if operation == "RESTORE" else set()
    ):
        raise ValueError("Invalid management fields")
    payload = {"path": path, "expected_revision": precondition["revision"], "operation": operation}
    if operation == "RESTORE":
        if not isinstance(value["document_id"], str):
            raise ValueError("Invalid recycled document identity")
        identifier = UUID(value["document_id"])
        if str(identifier) != value["document_id"] or identifier.int == 0:
            raise ValueError("Invalid recycled document identity")
        payload["document_id"] = str(identifier)
    if moving:
        destination = value["destination"]
        folder, _, name = str(destination).rpartition("/")
        if not isinstance(destination, str) or validate_document_path(
            project_id=project_id, folder=folder, name=name
        ) != (folder, name):
            raise ValueError("Invalid destination path")
        payload["destination"] = destination
    return payload
