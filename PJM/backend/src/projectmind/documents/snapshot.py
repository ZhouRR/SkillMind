"""明示的な文書選択と Run に凍結する文書集合の不変契約を定義する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast
from uuid import UUID

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.documents.domain import StoredDocument

DOCUMENT_PROVIDER = "project-documents"
DOCUMENT_READ_CAPABILITY = "document.read/v1"
ALL_DOCUMENTS_SELECTION = "project-documents:all"
MAX_SELECTED_DOCUMENTS = 5_000
DocumentSelectionMode = Literal["SINGLE", "SET", "ALL"]


class DocumentSnapshotError(ValueError):
    """文書選択または凍結内容を安全に利用できないことを表す。"""


@dataclass(frozen=True, slots=True)
class DocumentSelection:
    """利用者が明示的に選んだ範囲。ALL の集合は Run 作成時に確定する。"""

    mode: DocumentSelectionMode
    document_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class FrozenDocument:
    """再アップロードで同じ path が再利用されても入れ替わらない文書 identity。"""

    document_id: UUID
    folder: str
    name: str
    mime: str
    size: int
    content_hash: str

    @property
    def path(self) -> str:
        """Agent と manifest が共通に使う Project 相対 path を返す。"""

        return f"{self.folder}/{self.name}" if self.folder else self.name

    def to_json(self) -> dict[str, Any]:
        """blob locator を含めず、公開可能な凍結 metadata だけを返す。"""

        return {
            "document_id": str(self.document_id),
            "folder": self.folder,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    """Project・要求 slot・選択 mode と具体的な内容版を一緒に凍結する。"""

    project_id: UUID
    requirement_key: str
    selection_mode: DocumentSelectionMode
    documents: tuple[FrozenDocument, ...]

    def to_json(self) -> dict[str, Any]:
        """集合全体を単一 checksum で保護し、順序の違いで別版にしない。"""

        payload: dict[str, Any] = {
            "snapshot_version": "v1",
            "project_id": str(self.project_id),
            "requirement_key": self.requirement_key,
            "selection_mode": self.selection_mode,
            "documents": [item.to_json() for item in self.documents],
        }
        return {**payload, "checksum": f"sha256:{sha256_hex(canonical_json(payload))}"}


def parse_document_selection(token: str) -> DocumentSelection:
    """既存 sources の文字列契約内で単一・集合・明示的全集を一意に解釈する。"""

    if token == ALL_DOCUMENTS_SELECTION:
        return DocumentSelection("ALL", ())
    prefix, separator, raw_ids = token.partition(":")
    if not separator or prefix not in {"document", "documents"}:
        raise DocumentSnapshotError("An explicit document selection is required")
    pieces = raw_ids.split(",")
    if not 1 <= len(pieces) <= MAX_SELECTED_DOCUMENTS:
        raise DocumentSnapshotError("Document selection exceeds the allowed count")
    try:
        ids = tuple(UUID(value) for value in pieces)
    except ValueError as error:
        raise DocumentSnapshotError("Document selection contains an invalid identity") from error
    if len(set(ids)) != len(ids) or (prefix == "document" and len(ids) != 1):
        raise DocumentSnapshotError(
            "Document selection contains duplicate or unexpected identities"
        )
    if prefix == "documents" and len(ids) < 2:
        raise DocumentSnapshotError("A document set requires at least two identities")
    return DocumentSelection("SINGLE" if prefix == "document" else "SET", tuple(sorted(ids)))


def freeze_document_snapshot(
    *,
    project_id: UUID,
    requirement_key: str,
    selection: DocumentSelection,
    documents: Sequence[StoredDocument],
) -> DocumentSnapshot:
    """同一 transaction で取得した metadata から集合と内容 hash を凍結する。"""

    if not documents or len(documents) > MAX_SELECTED_DOCUMENTS:
        raise DocumentSnapshotError("Document selection is empty or exceeds the allowed count")
    if any(item.project_id != project_id for item in documents):
        raise DocumentSnapshotError("Selected document is not available in this project")
    frozen = tuple(
        FrozenDocument(
            document_id=item.document_id,
            folder=item.folder,
            name=item.name,
            mime=item.mime,
            size=item.size,
            content_hash=item.checksum,
        )
        for item in sorted(documents, key=lambda item: item.document_id)
    )
    if selection.mode != "ALL" and tuple(item.document_id for item in frozen) != (
        selection.document_ids
    ):
        raise DocumentSnapshotError("Selected document is not available in this project")
    snapshot = DocumentSnapshot(project_id, requirement_key, selection.mode, frozen)
    return parse_document_snapshot(
        snapshot.to_json(), project_id=project_id, requirement_key=requirement_key
    )


def parse_document_snapshot(
    value: Mapping[str, Any], *, project_id: UUID, requirement_key: str
) -> DocumentSnapshot:
    """Worker/Provider の入口で Project・slot・内容・checksum を同じ規則で再検証する。"""

    expected_fields = {
        "snapshot_version",
        "project_id",
        "requirement_key",
        "selection_mode",
        "documents",
        "checksum",
    }
    if set(value) != expected_fields or (
        value.get("snapshot_version") != "v1"
        or value.get("project_id") != str(project_id)
        or value.get("requirement_key") != requirement_key
    ):
        raise DocumentSnapshotError("Document snapshot identity is invalid")
    mode = value.get("selection_mode")
    if not isinstance(mode, str) or mode not in {"SINGLE", "SET", "ALL"}:
        raise DocumentSnapshotError("Document snapshot selection mode is invalid")
    entries = value.get("documents")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SELECTED_DOCUMENTS:
        raise DocumentSnapshotError("Document snapshot membership is invalid")
    documents = tuple(_parse_document(item) for item in entries)
    ids = tuple(item.document_id for item in documents)
    if ids != tuple(sorted(set(ids))) or len({item.path for item in documents}) != len(documents):
        raise DocumentSnapshotError("Document snapshot has duplicate or unordered members")
    if (mode == "SINGLE" and len(documents) != 1) or (mode == "SET" and len(documents) < 2):
        raise DocumentSnapshotError("Document snapshot membership does not match its mode")
    snapshot = DocumentSnapshot(
        project_id, requirement_key, cast(DocumentSelectionMode, mode), documents
    )
    if snapshot.to_json() != dict(value):
        raise DocumentSnapshotError("Document snapshot checksum is invalid")
    return snapshot


def selected_document_snapshots(
    selected_sources: Mapping[str, Any], *, project_id: UUID
) -> tuple[DocumentSnapshot, ...]:
    """文書 capability の全 slot を検証し、旧全集 snapshot へ暗黙退避しない。"""

    snapshots: list[DocumentSnapshot] = []
    for key, source in sorted(selected_sources.items()):
        if not isinstance(source, Mapping) or source.get("capability") != DOCUMENT_READ_CAPABILITY:
            continue
        value = source.get("document_snapshot")
        if source.get("provider") != DOCUMENT_PROVIDER or not isinstance(value, Mapping):
            raise DocumentSnapshotError("Run has no frozen document selection; create a new Run")
        snapshots.append(parse_document_snapshot(value, project_id=project_id, requirement_key=key))
    return tuple(snapshots)


def snapshot_documents(snapshots: Sequence[DocumentSnapshot]) -> tuple[FrozenDocument, ...]:
    """複数 slot の和集合だけを渡し、同じ identity/path の不整合は拒否する。"""

    unique: dict[UUID, FrozenDocument] = {}
    paths: dict[str, UUID] = {}
    for snapshot in snapshots:
        for item in snapshot.documents:
            if (item.document_id in unique and unique[item.document_id] != item) or (
                item.path in paths and paths[item.path] != item.document_id
            ):
                raise DocumentSnapshotError("Document snapshots contain conflicting members")
            unique[item.document_id] = item
            paths[item.path] = item.document_id
    return tuple(unique[key] for key in sorted(unique))


def _parse_document(value: Any) -> FrozenDocument:
    """移動や不正 path によって input/ 外へ書き込めない metadata だけを受理する。"""

    fields = {"document_id", "folder", "name", "mime", "size", "content_hash"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise DocumentSnapshotError("Frozen document metadata is invalid")
    try:
        document_id = UUID(value["document_id"])
    except (ValueError, TypeError, AttributeError) as error:
        raise DocumentSnapshotError("Frozen document identity is invalid") from error
    folder, name, mime, size, checksum = (
        value[key] for key in ("folder", "name", "mime", "size", "content_hash")
    )
    if (
        not isinstance(folder, str)
        or not isinstance(name, str)
        or not name
        or not isinstance(mime, str)
        or not mime
        or type(size) is not int
        or size < 0
        or not isinstance(checksum, str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", checksum) is None
    ):
        raise DocumentSnapshotError("Frozen document metadata is invalid")
    path = f"{folder}/{name}" if folder else name
    if (
        "/" in name
        or "\\" in path
        or not path.isprintable()
        or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", "..", ".projectmind"} for part in path.split("/"))
    ):
        raise DocumentSnapshotError("Frozen document path is invalid")
    return FrozenDocument(document_id, folder, name, mime, size, checksum)
