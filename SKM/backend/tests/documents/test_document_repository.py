"""DocumentRepository の Project 作用域と 404 折り畳みを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.db.models import ProjectDocument, ProjectDocumentCleanup
from skillmind.documents.domain import (
    DocumentCleanupActor,
    DocumentNotFoundError,
    DocumentStorageUnavailableError,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.storage import BlobReference, InMemoryFileStorage, StorageNamespace


class _GetSession:
    """session.get が固定行 (または None)を返す最小 seam。"""

    def __init__(self, document: ProjectDocument | None) -> None:
        """get が返す行と、delete された model を保持する。"""

        self._document = document
        self.deleted: list[object] = []
        self.added: list[object] = []

    def add(self, model: object) -> None:
        """目録消去前に同じ session へ保存された清理要求を観察する。"""

        self.added.append(model)

    async def get(self, model: object, identifier: object) -> ProjectDocument | None:
        """id に依らず設定した行を返す。"""

        del model, identifier
        return self._document

    async def delete(self, model: object) -> None:
        """削除された model を記録する。"""

        self.deleted.append(model)


def _document(project_id: object) -> ProjectDocument:
    """所有判定検証用の in-memory な文書行を作る。"""

    namespace = InMemoryFileStorage().namespace
    return ProjectDocument(
        id=uuid4(),
        project_id=project_id,
        folder="specs",
        name="overview.md",
        storage_key="projects/p/documents/d/overview.md",
        storage_namespace_id=namespace.namespace_id,
        storage_descriptor_checksum=namespace.descriptor_checksum,
        storage_is_durable=namespace.durable,
        size=10,
        mime="text/markdown",
        checksum="sha256:" + ("a" * 64),
        uploaded_by=uuid4(),
        created_at=datetime.now(UTC),
    )


def _cleanup_actor() -> DocumentCleanupActor:
    """repository の独立検証だけに使う原 DELETE 身份。認可は service 回帰で検証する。"""

    return DocumentCleanupActor(
        organization_id=uuid4(), actor_id=uuid4(), request_id=uuid4(), session_id=uuid4(),
    )


async def test_get_returns_document_for_owning_project() -> None:
    """所有 Project からは文書 metadata を取得できる。"""

    owner = uuid4()
    document = _document(owner)
    repository = DocumentRepository(_GetSession(document))  # type: ignore[arg-type]

    stored = await repository.get(project_id=owner, document_id=document.id)

    assert stored.document_id == document.id
    assert stored.project_id == owner


async def test_get_folds_cross_project_access_to_not_found() -> None:
    """他 Project の文書 ID を指定しても 404 相当へ畳む (存在を漏らさない)。"""

    document = _document(uuid4())
    repository = DocumentRepository(_GetSession(document))  # type: ignore[arg-type]

    with pytest.raises(DocumentNotFoundError):
        await repository.get(project_id=uuid4(), document_id=document.id)


async def test_delete_of_cross_project_document_is_not_found() -> None:
    """越権削除は 404 相当へ畳み、blob 削除用 key も返さない。"""

    session = _GetSession(_document(uuid4()))
    repository = DocumentRepository(session)  # type: ignore[arg-type]

    with pytest.raises(DocumentNotFoundError):
        await repository.delete(
            project_id=uuid4(), document_id=uuid4(), cleanup_actor=_cleanup_actor(),
        )
    assert session.deleted == []
    assert session.added == []


async def test_delete_of_owned_document_returns_original_blob_reference() -> None:
    """所有文書の削除は行を消し、元 key と namespace を分離せず返す。"""

    owner = uuid4()
    document = _document(owner)
    session = _GetSession(document)
    repository = DocumentRepository(session)  # type: ignore[arg-type]

    actor = _cleanup_actor()
    reference = await repository.delete(
        project_id=owner, document_id=document.id, cleanup_actor=actor,
    )

    assert document.storage_namespace_id is not None
    assert document.storage_descriptor_checksum is not None
    assert document.storage_is_durable is not None
    assert reference == BlobReference(
        document.storage_key,
        StorageNamespace(
            document.storage_namespace_id,
            document.storage_descriptor_checksum,
            document.storage_is_durable,
        ),
    )
    assert session.deleted == [document]
    assert len(session.added) == 1
    cleanup = session.added[0]
    assert isinstance(cleanup, ProjectDocumentCleanup)
    assert cleanup.document_id == document.id and cleanup.source_protocol == "LEGACY_UNVERIFIED"
    assert cleanup.upload_intent_id is None and cleanup.size == document.size
    assert cleanup.request_id == actor.request_id and cleanup.session_id == actor.session_id


@pytest.mark.parametrize("bound", [False, True])
async def test_download_and_path_lookup_preserve_original_namespace(bound: bool) -> None:
    """旧行の None も明示して返し、path lookup が現在 storage の所属を補わない。"""

    owner = uuid4()
    document = _document(owner)
    if not bound:
        document.storage_namespace_id = None
        document.storage_descriptor_checksum = None
        document.storage_is_durable = None
    session = MagicMock()
    session.get = AsyncMock(return_value=document)
    rows = MagicMock()
    rows.first.return_value = document
    session.scalars = AsyncMock(return_value=rows)
    repository = DocumentRepository(session)

    downloaded = await repository.get_for_download(project_id=owner, document_id=document.id)
    found = await repository.find_by_path(
        project_id=owner, folder=document.folder, name=document.name
    )

    assert found == downloaded
    metadata, reference = downloaded
    assert metadata.document_id == document.id and reference.key == document.storage_key
    if bound:
        assert document.storage_namespace_id is not None
        assert document.storage_descriptor_checksum is not None
        assert document.storage_is_durable is not None
        assert reference.namespace == StorageNamespace(
            document.storage_namespace_id,
            document.storage_descriptor_checksum,
            document.storage_is_durable,
        )
    else:
        assert reference.namespace is None
    statement = session.scalars.await_args.args[0]
    assert statement.compile().params == {
        "project_id_1": owner,
        "folder_1": document.folder,
        "name_1": document.name,
    }


@pytest.mark.parametrize("operation", ["download", "path", "delete"])
@pytest.mark.parametrize("descriptor", [None, "invalid"])
async def test_corrupt_namespace_is_not_projected_as_an_unbound_reference(
    operation: str,
    descriptor: str | None,
) -> None:
    """部分保存と壊れた値を旧行の None に折り畳まず、削除を含む全参照経路で拒否する。"""

    owner = uuid4()
    document = _document(owner)
    document.storage_descriptor_checksum = descriptor
    session = MagicMock()
    session.get = AsyncMock(return_value=document)
    session.delete = AsyncMock()
    rows = MagicMock()
    rows.first.return_value = document
    session.scalars = AsyncMock(return_value=rows)
    repository = DocumentRepository(session)

    with pytest.raises(DocumentStorageUnavailableError):
        if operation == "download":
            await repository.get_for_download(project_id=owner, document_id=document.id)
        elif operation == "path":
            await repository.find_by_path(
                project_id=owner, folder=document.folder, name=document.name
            )
        else:
            await repository.delete(
                project_id=owner, document_id=document.id, cleanup_actor=_cleanup_actor(),
            )

    session.delete.assert_not_awaited()
    assert document.storage_descriptor_checksum == descriptor
