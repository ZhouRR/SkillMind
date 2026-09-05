"""DocumentRepository の Project 作用域と 404 折り畳みを検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from projectmind.db.models import ProjectDocument
from projectmind.documents import DocumentNotFoundError, DocumentRepository


class _GetSession:
    """session.get が固定行 (または None)を返す最小 seam。"""

    def __init__(self, document: ProjectDocument | None) -> None:
        """get が返す行と、delete された model を保持する。"""

        self._document = document
        self.deleted: list[object] = []

    async def get(self, model: object, identifier: object) -> ProjectDocument | None:
        """id に依らず設定した行を返す。"""

        del model, identifier
        return self._document

    async def delete(self, model: object) -> None:
        """削除された model を記録する。"""

        self.deleted.append(model)


def _document(project_id: object) -> ProjectDocument:
    """所有判定検証用の in-memory な文書行を作る。"""

    return ProjectDocument(
        id=uuid4(),
        project_id=project_id,
        folder="specs",
        name="overview.md",
        storage_key="projects/p/documents/d/overview.md",
        size=10,
        mime="text/markdown",
        checksum="sha256:" + ("a" * 64),
        uploaded_by=uuid4(),
        created_at=datetime.now(UTC),
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
        await repository.delete(project_id=uuid4(), document_id=uuid4())
    assert session.deleted == []


async def test_delete_of_owned_document_returns_storage_key() -> None:
    """所有文書の削除は行を消し、blob 削除用の storage_key を返す。"""

    owner = uuid4()
    document = _document(owner)
    session = _GetSession(document)
    repository = DocumentRepository(session)  # type: ignore[arg-type]

    storage_key = await repository.delete(project_id=owner, document_id=document.id)

    assert storage_key == document.storage_key
    assert session.deleted == [document]
