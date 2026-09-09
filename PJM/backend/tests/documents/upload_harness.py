"""既存の削除/認証 SQL fake を共有し、upload の二段階 transaction を検証する。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import Select, func

from projectmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectDocument,
    ProjectMember,
    User,
)
from projectmind.documents.domain import StoredDocument
from projectmind.documents.service import DocumentService
from projectmind.storage import InMemoryFileStorage, StoredBlob, UploadLimits
from tests.documents.deletion_harness import DeletionDatabase

UPLOAD_LIMITS = UploadLimits(
    max_bytes=1_000_000,
    project_quota_bytes=2_000_000,
    allowed_content_types=frozenset({"text/plain", "text/markdown", "image/png"}),
)


class UploadDatabase(DeletionDatabase):
    """認証を迂回せず、実 repository SQL と lock 外の memory storage を結ぶ。"""

    def __init__(self, *, limits: UploadLimits = UPLOAD_LIMITS) -> None:
        """文書 rollback は親 fake に任せ、PUT 待機と用量読取の hook だけを追加する。"""

        super().__init__()
        self.documents = []
        self.blobs = InMemoryFileStorage()
        self.on_put: Callable[[], None] | None = None
        self.on_usage: Callable[[], None] | None = None
        self.usage_reads = 0
        self.lock_transaction = 0
        self.lock_index = 0
        self.storage.put = AsyncMock(side_effect=self.put_blob)
        self.document_service = DocumentService(
            self.session_factory, file_storage=self.storage, limits=limits
        )

    def observe_lock(self, statement: Select[Any]) -> None:
        """Org UPDATE → User SHARE → 原 Session UPDATE → Project/所属 SHARE を守る。"""

        lock = statement._for_update_arg
        assert self.in_transaction and lock is not None
        entity = statement.column_descriptions[0]["entity"]
        expected = [Organization, User, AuthSession, Project, ProjectMember]
        if self.lock_transaction != self.transactions:
            self.lock_transaction = self.transactions
            self.lock_index = 0
        assert entity is expected[self.lock_index]
        self.lock_index += 1
        assert lock.read is (entity in (User, Project, ProjectMember))
        if entity is not Organization:
            assert statement.get_execution_options()["populate_existing"] is True
        super().observe_lock(statement)

    async def scalar(self, statement: Select[Any]) -> Any:
        """quota は正確 Project の全 metadata size を、同じ認証 transaction 内で合算する。"""

        description = statement.column_descriptions[0]
        if description["entity"] is not ProjectDocument:
            return await super().scalar(statement)
        assert self.in_transaction and statement._for_update_arg is None
        assert description["expr"].compare(func.coalesce(func.sum(ProjectDocument.size), 0))
        params = statement.compile().params
        assert set(params) == {"coalesce_2", "project_id_1"}
        assert params["coalesce_2"] == 0 and params["project_id_1"] == self.project.id
        assert statement.whereclause is not None and statement.whereclause.compare(
            ProjectDocument.project_id == self.project.id
        )
        self.statements.append(statement)
        self.usage_reads += 1
        if self.on_usage:
            self.on_usage()
        return sum(row.size for row in self.documents if row.project_id == self.project.id)

    def add(self, row: Any) -> None:
        """保存対象の Project と actor を固定し、合成 rollback 対象へ行を追加する。"""

        if not isinstance(row, ProjectDocument):
            super().add(row)
            return
        assert self.in_transaction and self.transactions == 2
        assert row.project_id == self.project.id and row.uploaded_by == self.user.id
        self.documents.append(row)

    async def put_blob(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """初回 commit が確定した lock 外でのみ byte を保存し、その後の撤権を注入する。"""

        assert not self.in_transaction and self.commits == 1 and not self.commit_error
        assert type(data) is bytes
        result = await self.blobs.put(key, data, content_type=content_type)
        if self.on_put:
            self.on_put()
        return result

    async def upload(
        self,
        *,
        folder: str = "",
        name: str = "note.txt",
        data: bytes = b"hello",
        content_type: str = "text/plain",
    ) -> StoredDocument:
        """呼出元が uploaded_by を偽装できない公開 service 入口を利用する。"""

        return await self.document_service.upload_document(
            project_id=self.project.id,
            access=self.access,
            folder=folder,
            name=name,
            data=data,
            content_type=content_type,
        )
