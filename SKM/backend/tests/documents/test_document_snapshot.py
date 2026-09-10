"""明示的な文書集合の凍結・改変拒否・内容取得境界を検証する。"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from skillmind.documents.binding import resolve_document_binding
from skillmind.documents.domain import DocumentNotFoundError, StoredDocument
from skillmind.documents.snapshot import (
    ALL_DOCUMENTS_SELECTION,
    DOCUMENT_READ_CAPABILITY,
    DocumentSelection,
    DocumentSnapshotError,
    freeze_document_snapshot,
    parse_document_selection,
    parse_document_snapshot,
    selected_document_snapshots,
    snapshot_documents,
)
from skillmind.documents.source import ProjectDocumentContent, read_frozen_document
from tests.documents.fakes import document_content, document_snapshot, stored_document


@pytest.mark.parametrize(
    "token",
    [
        "project",
        "project-documents",
        "specs/a.md",
        "document:nope",
        "documents:",
        "documents:" + str(UUID(int=1)),
        "document:" + str(UUID(int=1)) + "," + str(UUID(int=2)),
        "documents:" + str(UUID(int=1)) + "," + str(UUID(int=1)),
    ],
)
def test_implicit_or_malformed_document_selection_is_rejected(token: str) -> None:
    """Provider 名や path だけを全集への同意と見なさない。"""

    with pytest.raises(DocumentSnapshotError):
        parse_document_selection(token)


def test_explicit_selection_modes_have_canonical_membership() -> None:
    """単一・集合・全集を区別し、集合の ID 順序を正規化する。"""

    first, second = UUID(int=1), UUID(int=2)
    assert parse_document_selection(f"document:{first}") == DocumentSelection("SINGLE", (first,))
    assert parse_document_selection(f"documents:{second},{first}") == DocumentSelection(
        "SET", (first, second)
    )
    assert parse_document_selection(ALL_DOCUMENTS_SELECTION) == DocumentSelection("ALL", ())


def test_snapshot_freezes_ids_paths_hashes_and_round_trips() -> None:
    """作成時の集合から、後で Project を再列挙しなくてよい完全な snapshot を作る。"""

    project_id = uuid4()
    content = document_content()
    document = stored_document(project_id, content)
    snapshot = freeze_document_snapshot(
        project_id=project_id,
        requirement_key="design",
        documents=[document],
        selection=parse_document_selection(f"document:{document.document_id}"),
    )
    assert snapshot.documents[0].document_id == document.document_id
    assert snapshot.documents[0].content_hash == content.checksum
    assert snapshot.documents[0].path == "specs/overview.md"
    assert (
        parse_document_snapshot(snapshot.to_json(), project_id=project_id, requirement_key="design")
        == snapshot
    )


def test_snapshot_rejects_project_slot_and_content_changes() -> None:
    """別 Project/slot への流用と hash の差し替えを同じ境界で拒否する。"""

    project_id = uuid4()
    snapshot = document_snapshot(project_id, [document_content()])
    for changed_project, key in ((uuid4(), "config"), (project_id, "another")):
        with pytest.raises(DocumentSnapshotError):
            parse_document_snapshot(
                snapshot.to_json(), project_id=changed_project, requirement_key=key
            )
    value = snapshot.to_json()
    value["documents"][0]["name"] = "changed.md"
    with pytest.raises(DocumentSnapshotError, match="checksum"):
        parse_document_snapshot(value, project_id=project_id, requirement_key="config")


def test_snapshot_cannot_include_unselected_or_foreign_members() -> None:
    """freeze に余分な文書が渡っても、選択より広い集合を受け入れない。"""

    project_id = uuid4()
    one = stored_document(project_id, document_content())
    two = stored_document(project_id, document_content(name="another.md"))
    selection = parse_document_selection(f"document:{one.document_id}")
    for documents in ([one, two], [replace(one, project_id=uuid4())]):
        with pytest.raises(DocumentSnapshotError):
            freeze_document_snapshot(
                project_id=project_id,
                requirement_key="config",
                selection=selection,
                documents=documents,
            )


def test_legacy_document_source_has_no_implicit_all_permission() -> None:
    """古い Run の provider だけの指定から現在の Project 全集を合成しない。"""

    with pytest.raises(DocumentSnapshotError, match="create a new Run"):
        selected_document_snapshots(
            {"config": {"capability": DOCUMENT_READ_CAPABILITY, "provider": "project"}},
            project_id=uuid4(),
        )


def test_conflicting_snapshots_cannot_merge_different_versions() -> None:
    """複数 slot が同じ文書の別内容を指す不整合は黙って片方を採用しない。"""

    project_id = uuid4()
    first = document_snapshot(project_id, [document_content()])
    second = document_snapshot(project_id, [document_content(b"new contents")], key="other")
    with pytest.raises(DocumentSnapshotError):
        snapshot_documents([first, second])


class _DocumentRepository:
    """Project 所有権と列挙の時点を検証できる metadata repository。"""

    def __init__(self, documents: list[StoredDocument]) -> None:
        """内容を書き換えず、可視な metadata 集合だけを保持する。"""

        self.documents = documents

    async def list_for_project(self, project_id: UUID) -> list[StoredDocument]:
        """現在の Project 所有集合を一度の列挙として返す。"""

        return [item for item in self.documents if item.project_id == project_id]

    async def get(self, *, project_id: UUID, document_id: UUID) -> StoredDocument:
        """別 Project の ID を名前に関係なく拒否する。"""

        found = next(
            (
                item
                for item in self.documents
                if (item.project_id, item.document_id) == (project_id, document_id)
            ),
            None,
        )
        if found is None:
            raise DocumentNotFoundError("not found")
        return found


async def test_all_selection_freezes_membership_before_later_uploads() -> None:
    """ALL は将来の追加文書への動的権限ではない。"""

    project_id = uuid4()
    first = stored_document(project_id, document_content())
    repository = _DocumentRepository([first])
    binding = await resolve_document_binding(
        repository, project_id=project_id, requirement_key="config", token=ALL_DOCUMENTS_SELECTION
    )  # type: ignore[arg-type]
    repository.documents.append(stored_document(project_id, document_content(name="later.md")))
    frozen = selected_document_snapshots({"config": binding}, project_id=project_id)
    assert [item.document_id for item in snapshot_documents(frozen)] == [first.document_id]


class _ContentSource:
    """指定 identity の内容だけを返し、呼び出し先を記録する source。"""

    def __init__(self, content: ProjectDocumentContent | None) -> None:
        """欠落・改変された source も同じ seam で表現する。"""

        self.content = content
        self.requests: list[UUID] = []

    async def fetch(self, *, project_id: UUID, document_id: UUID) -> ProjectDocumentContent | None:
        """path ではなく凍結 ID で問い合わせられたことを記録する。"""

        del project_id
        self.requests.append(document_id)
        return self.content


async def test_read_checks_actual_bytes_not_only_storage_metadata() -> None:
    """metadata hash が同じでも blob 本文の改変は検出する。"""

    project_id = uuid4()
    content = document_content()
    frozen = document_snapshot(project_id, [content]).documents[0]
    source = _ContentSource(replace(content, data=b"x" * content.size))
    with pytest.raises(DocumentSnapshotError, match="content"):
        await read_frozen_document(source, project_id=project_id, document=frozen)
    assert source.requests == [content.document_id]


async def test_deleted_or_replaced_document_cannot_supply_a_new_version() -> None:
    """削除と同名再 upload は元文書の代わりにならない。"""

    project_id = uuid4()
    content = document_content()
    frozen = document_snapshot(project_id, [content]).documents[0]
    for replacement in (None, replace(content, document_id=uuid4())):
        with pytest.raises(DocumentSnapshotError):
            await read_frozen_document(
                _ContentSource(replacement), project_id=project_id, document=frozen
            )
