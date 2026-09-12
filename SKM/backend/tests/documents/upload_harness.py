"""既存の削除/認証 SQL fake を共有し、upload の二段階 transaction を検証する。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, closing
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from sqlalchemy import Select, and_
from sqlalchemy.dialects import sqlite
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BooleanClauseList, Case
from sqlalchemy.sql.selectable import Exists

from skillmind.db.models import (
    AuthSession,
    Organization,
    Project,
    ProjectDocument,
    ProjectDocumentEffectUpload,
    ProjectDocumentUpload,
    ProjectDocumentUploadClosure,
    ProjectMember,
    User,
)
from skillmind.documents.domain import StoredDocument
from skillmind.documents.service import DocumentService
from skillmind.storage import InMemoryFileStorage, StoredBlob, UploadLimits
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
        self.storage.namespace = self.blobs.namespace
        self.on_put: Callable[[], None] | None = None
        self.on_usage: Callable[[], None] | None = None
        self.usage_reads = 0
        self.last_upload_key: UUID | None = None
        self.closures: list[ProjectDocumentUploadClosure] = []
        self.effects: list[ProjectDocumentEffectUpload] = []
        self.transaction_gate = asyncio.Lock()
        self.on_closure_read: Callable[[], None] | None = None
        self.lock_transaction = 0
        self.lock_index = 0
        self.storage.put = AsyncMock(side_effect=self.put_blob)
        self.storage.delete = AsyncMock(side_effect=self.delete_uploaded_blob)
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
        if self.lock_index < len(expected) and not (
            self.lock_index == 4 and self.user.system_role == "ADMIN"
        ):
            assert entity is expected[self.lock_index]
        else:
            assert entity in (ProjectDocument, ProjectDocumentUpload)
        self.lock_index += 1
        assert lock.read is (entity in (User, Project, ProjectMember))
        if entity is not Organization:
            assert statement.get_execution_options()["populate_existing"] is True
        super().observe_lock(statement)

    async def scalar(self, statement: Select[Any]) -> Any:
        """新 query は実 SQL を局部評価し、認証や旧文書 lock の判定を弱めない。"""

        description = statement.column_descriptions[0]
        expression = description["expr"]
        if expression is ProjectDocumentUploadClosure:
            assert self.in_transaction and statement._for_update_arg is None
            assert statement.get_execution_options()["populate_existing"] is True
            params = statement.compile().params
            assert set(params) == {"upload_intent_id_1"}
            assert statement.whereclause is not None and statement.whereclause.compare(
                ProjectDocumentUploadClosure.upload_intent_id == params["upload_intent_id_1"]
            )
            self.statements.append(statement)
            if self.on_closure_read:
                self.on_closure_read()
            return next(
                (
                    row
                    for row in self.closures
                    if row.upload_intent_id == params["upload_intent_id_1"]
                ),
                None,
            )
        if isinstance(expression, Exists):
            return self.path_exists(statement, expression)
        if isinstance(expression, BooleanClauseList):
            assert expression.operator is operators.or_
            return any(self.path_exists(Select(clause), clause) for clause in expression.clauses)
        if not isinstance(expression, Case):
            return await super().scalar(statement)
        assert self.in_transaction and statement._for_update_arg is None
        self.statements.append(statement)
        self.usage_reads += 1
        if self.on_usage:
            self.on_usage()
        # 実 SQL の WHERE/CASE/SUM を評価し、test 専用の課金算法へ置き換えない。
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute(
                "CREATE TABLE project_documents "
                "(project_id TEXT, size INTEGER, upload_intent_id TEXT, effect_upload_id TEXT)"
            )
            database.execute("CREATE TABLE document_upload_intents (project_id TEXT, size INTEGER)")
            database.execute("CREATE TABLE document_effect_uploads (project_id TEXT, size INTEGER)")
            database.executemany("INSERT INTO document_effect_uploads VALUES (?, ?)",
                                 [(row.project_id.hex, row.size) for row in self.effects])
            database.execute(
                "CREATE TABLE document_blob_cleanups "
                "(project_id TEXT, size INTEGER, upload_intent_id TEXT)"
            )
            database.executemany(
                "INSERT INTO project_documents VALUES (?, ?, ?, ?)",
                [
                    (
                        row.project_id.hex,
                        row.size,
                        (None if row.upload_intent_id is None else row.upload_intent_id.hex),
                        (None if row.effect_upload_id is None else row.effect_upload_id.hex),
                    )
                    for row in self.documents
                ],
            )
            database.executemany(
                "INSERT INTO document_upload_intents VALUES (?, ?)",
                [(row.project_id.hex, row.size) for row in self.intents],
            )
            database.executemany(
                "INSERT INTO document_blob_cleanups VALUES (?, ?, ?)",
                [
                    (
                        row.project_id.hex,
                        row.size,
                        (None if row.upload_intent_id is None else row.upload_intent_id.hex),
                    )
                    for row in self.cleanups
                ],
            )
            sql = str(
                statement.compile(
                    dialect=sqlite.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            result = database.execute(sql).fetchone()
            assert result is not None
            return result[0]

    def path_exists(self, statement: Select[Any], expression: Exists) -> bool:
        """実 EXISTS が正確 path/PENDING だけを判定することを構造比較で守る。"""

        assert self.in_transaction and statement._for_update_arg is None
        inner = expression.element.element
        table = inner.get_final_froms()[0].name
        params = statement.compile().params
        self.statements.append(statement)
        model: type[ProjectDocument] | type[ProjectDocumentUpload]
        rows: Sequence[ProjectDocument | ProjectDocumentUpload]
        if table == "project_documents":
            model, rows = ProjectDocument, self.documents
            assert set(params) == {"project_id_1", "folder_1", "name_1"}
        elif table == "document_effect_uploads":
            model, rows = ProjectDocumentEffectUpload, self.effects
            assert set(params) == {"project_id_1", "folder_1", "name_1"}
        else:
            assert table == "document_upload_intents"
            model, rows = ProjectDocumentUpload, self.intents
            assert set(params) == {"project_id_1", "folder_1", "name_1", "state_1"}
            assert params["state_1"] == "PENDING"
        clauses = [
            model.project_id == params["project_id_1"],
            model.folder == params["folder_1"],
            model.name == params["name_1"],
        ]
        if model is ProjectDocumentUpload:
            clauses.append(ProjectDocumentUpload.state == "PENDING")
            clauses.append(ProjectDocumentUpload.publication_closed_at.is_(None))
        assert inner.whereclause is not None and inner.whereclause.compare(and_(*clauses))
        return any(
            row.project_id == params["project_id_1"]
            and row.folder == params["folder_1"]
            and row.name == params["name_1"]
            and (
                not isinstance(row, ProjectDocumentUpload)
                or (row.state == "PENDING" and row.publication_closed_at is None)
            )
            for row in rows
        )

    def add(self, row: Any) -> None:
        """保存対象の Project と actor を固定し、合成 rollback 対象へ行を追加する。"""

        if isinstance(row, ProjectDocumentUploadClosure):
            assert self.in_transaction and row.project_id == self.project.id
            assert row.organization_id == self.user.organization_id
            assert row.actor_id == row.requested_by == self.user.id
            assert row.request_id == self.access.request_id
            assert row.session_id == self.auth_session.id
            assert not any(item.upload_intent_id == row.upload_intent_id for item in self.closures)
            assert any(
                intent.id == row.upload_intent_id
                and intent.document_id == row.document_id
                and intent.project_id == row.project_id
                and intent.upload_key == row.upload_key
                and intent.state == "PENDING"
                for intent in self.intents
            )
            self.closures.append(row)
            return
        if isinstance(row, ProjectDocumentUpload):
            assert self.in_transaction and row.project_id == self.project.id
            assert row.actor_id == self.user.id and row.state == "PENDING"
            assert row.original_session_id == self.auth_session.id
            self.intents.append(row)
            return
        if not isinstance(row, ProjectDocument):
            super().add(row)
            return
        assert self.in_transaction
        assert row.project_id == self.project.id and row.uploaded_by == self.user.id
        assert any(
            intent.id == row.upload_intent_id
            and intent.document_id == row.id
            and intent.project_id == row.project_id
            and intent.state == "PENDING"
            for intent in self.intents
        )
        self.documents.append(row)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        """同組織 gate の直列化を合成し、原 SQL の lock 順序と監査 rollback を併せて守る。

        実 PostgreSQL の lock 待機・制約ではない。最初の Org UPDATE は observe_lock が必須化する。
        """

        async with self.transaction_gate:
            original = list(self.closures)
            commits = self.commits
            snapshots = {
                row.id: {
                    column.name: deepcopy(getattr(row, column.name))
                    for column in ProjectDocumentUploadClosure.__table__.columns
                }
                for row in original
            }
            try:
                async with super().transaction() as repository:
                    yield repository
            except BaseException:
                if self.commits == commits:
                    self.closures = original
                    for row in original:
                        for name, value in snapshots[row.id].items():
                            setattr(row, name, value)
                raise

    async def put_blob(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """初回 commit が確定した lock 外でのみ byte を保存し、その後の撤権を注入する。"""

        assert not self.in_transaction and self.commits > 0 and not self.commit_error
        assert any(row.storage_key == key and row.state == "PENDING" for row in self.intents)
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
        upload_key: UUID | None = None,
    ) -> StoredDocument:
        """呼出元が uploaded_by を偽装できない公開 service 入口を利用する。"""

        self.last_upload_key = upload_key or uuid4()
        return await self.document_service.upload_document(
            project_id=self.project.id,
            access=self.access,
            upload_key=self.last_upload_key,
            folder=folder,
            name=name,
            data=data,
            content_type=content_type,
        )

    async def delete_uploaded_blob(self, key: str) -> None:
        """任意の成功 transaction 後だけ精確 key を削除し、意図の占用は変更しない。"""

        assert not self.in_transaction and self.commits > 0 and not self.commit_error
        assert any(row.storage_key == key for row in self.cleanups)
        await self.blobs.delete(key)
