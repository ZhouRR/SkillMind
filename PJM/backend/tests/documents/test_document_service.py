"""DocumentService の upload 安全境界と object storage 連携を検証する。"""

from __future__ import annotations

from typing import Self
from uuid import uuid4

import pytest

from projectmind.db.models import ProjectDocument
from projectmind.documents.service import DocumentService
from projectmind.storage import InMemoryFileStorage, UploadLimits, UploadRejectedError

_LIMITS = UploadLimits(
    max_bytes=1_000_000,
    project_quota_bytes=2_000_000,
    allowed_content_types=frozenset({"text/plain", "text/markdown", "image/png"}),
)


class _Transaction:
    """Test session の async transaction context。"""

    async def __aenter__(self) -> Self:
        """Transaction context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず transaction context を終了する。"""


class _FakeSession:
    """DocumentRepository が使う最小 AsyncSession seam (statement 内容は無視)。"""

    def __init__(self, *, usage: int) -> None:
        """配額判定に返す usage と、追加された model を保持する。"""

        self._usage = usage
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        """Session context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず Session context を終了する。"""

    def begin(self) -> _Transaction:
        """Service が所有する transaction context を返す。"""

        return _Transaction()

    async def scalar(self, statement: object) -> int:
        """usage 合計問い合わせに固定値を返す。"""

        del statement
        return self._usage

    def add(self, model: object) -> None:
        """Repository が追加した model を記録する。"""

        self.added.append(model)


class _FakeSessionFactory:
    """同じ test session を返す session factory。"""

    def __init__(self, session: _FakeSession) -> None:
        """Use case 後に追加 model を確認する session を保持する。"""

        self._session = session

    def __call__(self) -> _FakeSession:
        """Async context 対応 session を返す。"""

        return self._session


def _service(session: _FakeSession, storage: InMemoryFileStorage) -> DocumentService:
    """Fake session と in-memory storage で DocumentService を組み立てる。"""

    return DocumentService(
        _FakeSessionFactory(session),  # type: ignore[arg-type]
        file_storage=storage,
        limits=_LIMITS,
    )


async def test_upload_stores_blob_and_persists_normalized_metadata() -> None:
    """許可 upload は blob を object storage へ保存し、正規化 metadata を永続化する。"""

    session = _FakeSession(usage=0)
    storage = InMemoryFileStorage()
    project_id = uuid4()

    stored = await _service(session, storage).upload_document(
        project_id=project_id,
        uploaded_by=uuid4(),
        folder="specs/",
        name="overview.md",
        data=b"# Overview\n",
        content_type="text/markdown; charset=utf-8",
    )

    assert stored.name == "overview.md"
    assert stored.folder == "specs"  # 前後 slash を落として正規化する。
    assert stored.mime == "text/markdown"  # content-type parameter を落とす。
    assert stored.checksum.startswith("sha256:")
    document = next(model for model in session.added if isinstance(model, ProjectDocument))
    # blob は project 作用域の key で object storage に round-trip できる。
    assert document.storage_key.startswith(f"projects/{project_id}/documents/")
    assert await storage.get(document.storage_key) == b"# Overview\n"


async def test_upload_rejects_quota_exceeded_before_storage_write() -> None:
    """既存 usage と合算で配額を超える upload は storage 前に fail-closed で拒否する。"""

    session = _FakeSession(usage=_LIMITS.project_quota_bytes)
    storage = InMemoryFileStorage()

    with pytest.raises(UploadRejectedError) as excinfo:
        await _service(session, storage).upload_document(
            project_id=uuid4(),
            uploaded_by=uuid4(),
            folder="",
            name="note.txt",
            data=b"hello",
            content_type="text/plain",
        )
    assert excinfo.value.code == "project_quota_exceeded"
    assert session.added == []


async def test_upload_rejects_credential_like_text_content() -> None:
    """text 系 upload に credential らしい本文が含まれれば拒否する。"""

    session = _FakeSession(usage=0)

    with pytest.raises(UploadRejectedError) as excinfo:
        await _service(session, InMemoryFileStorage()).upload_document(
            project_id=uuid4(),
            uploaded_by=uuid4(),
            folder="",
            name="secret.txt",
            data=b"api_key = ABC123SECRET",
            content_type="text/plain",
        )
    assert excinfo.value.code == "sensitive_content"


async def test_upload_rejects_unsafe_document_name() -> None:
    """path segment を含む文書名は単一 file 名でないため拒否する。"""

    session = _FakeSession(usage=0)

    with pytest.raises(UploadRejectedError) as excinfo:
        await _service(session, InMemoryFileStorage()).upload_document(
            project_id=uuid4(),
            uploaded_by=uuid4(),
            folder="",
            name="../escape.txt",
            data=b"x",
            content_type="text/plain",
        )
    assert excinfo.value.code == "invalid_document_name"
