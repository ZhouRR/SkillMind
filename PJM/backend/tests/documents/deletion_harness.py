"""実 service/repository を明示 SQL と合成 commit で検証する。実 DB 並行性は証明しない。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from sqlalchemy import Select, and_, or_
from sqlalchemy.sql.selectable import Join

from projectmind.db.models import (
    ProjectDocument,
    ProjectDocumentCleanup,
    ProjectDocumentUpload,
    Run,
    TaskSchedule,
    TaskScheduleOccurrence,
)
from projectmind.documents.repository import DocumentRepository
from projectmind.documents.service import DocumentService
from projectmind.storage import InMemoryFileStorage, UploadLimits
from tests.documents.fakes import document_content, stored_document
from tests.schedules.authorization_harness import ScheduleAuthorizationDatabase


class DeletionDatabase(ScheduleAuthorizationDatabase):
    """共通認証 fake の SQL 判定を保ち、文書参照 query だけを追加する。"""

    def __init__(self) -> None:
        """合成文書の一行と、成功 commit 外のみ実行可能な blob spy を準備する。"""

        super().__init__()
        data = stored_document(self.project.id, document_content())
        namespace = InMemoryFileStorage().namespace
        self.document = ProjectDocument(
            id=data.document_id,
            project_id=data.project_id,
            folder=data.folder,
            name=data.name,
            size=data.size,
            mime=data.mime,
            checksum=data.checksum,
            uploaded_by=data.uploaded_by,
            created_at=data.created_at,
            storage_key=f"projects/{data.project_id}/{data.document_id}",
            storage_namespace_id=namespace.namespace_id,
            storage_descriptor_checksum=namespace.descriptor_checksum,
            storage_is_durable=namespace.durable,
        )
        self.documents = [self.document]
        self.intents: list[ProjectDocumentUpload] = []
        self.cleanups: list[ProjectDocumentCleanup] = []
        self.session.get = AsyncMock(side_effect=self.get)
        self.session.delete = AsyncMock(side_effect=self.delete)
        self.session.execute = AsyncMock(side_effect=self.execute)
        self.storage = MagicMock()
        self.storage.namespace = namespace
        self.storage.delete = AsyncMock(side_effect=self.delete_blob)
        self.document_service = DocumentService(
            self.session_factory,
            file_storage=self.storage,
            limits=UploadLimits(1_000_000, 2_000_000, frozenset({"text/markdown"})),
        )

    async def scalar(self, statement: Select[Any]) -> Any:
        """文書 lock は精確 Project/ID 条件と post-lock refresh を必要とする。"""

        entity = statement.column_descriptions[0]["entity"]
        if entity is ProjectDocumentUpload:
            self.observe_lock(statement)
            self.statements.append(statement)
            assert self.in_transaction and statement._for_update_arg is not None
            assert not statement._for_update_arg.read
            assert statement.get_execution_options()["populate_existing"] is True
            params = statement.compile().params
            if set(params) == {"id_1"}:
                expected = ProjectDocumentUpload.id == params["id_1"]
                candidates = [row for row in self.intents if row.id == params["id_1"]]
            else:
                assert set(params) == {
                    "organization_id_1", "project_id_1", "actor_id_1", "upload_key_1",
                }
                expected = and_(
                    ProjectDocumentUpload.organization_id == params["organization_id_1"],
                    ProjectDocumentUpload.project_id == params["project_id_1"],
                    ProjectDocumentUpload.actor_id == params["actor_id_1"],
                    ProjectDocumentUpload.upload_key == params["upload_key_1"],
                )
                candidates = [
                    row for row in self.intents
                    if row.organization_id == params["organization_id_1"]
                    and row.project_id == params["project_id_1"]
                    and row.actor_id == params["actor_id_1"]
                    and row.upload_key == params["upload_key_1"]
                ]
            assert statement.whereclause is not None and statement.whereclause.compare(expected)
            return next(iter(candidates), None)
        if entity is not ProjectDocument:
            return await super().scalar(statement)
        self.observe_lock(statement)
        self.statements.append(statement)
        params = statement.compile().params
        assert set(params) == {"project_id_1", "id_1"}
        assert statement.whereclause is not None and statement.whereclause.compare(
            and_(
                ProjectDocument.project_id == params["project_id_1"],
                ProjectDocument.id == params["id_1"],
            )
        )
        assert statement._for_update_arg is not None and not statement._for_update_arg.read
        assert statement.get_execution_options()["populate_existing"] is True
        return next(
            (
                row
                for row in self.documents
                if row.id == params["id_1"] and row.project_id == params["project_id_1"]
            ),
            None,
        )

    async def scalars(self, statement: Select[Any]) -> Any:
        """参照 scan は全状態・精確 Project・無行 lock とし、弱い WHERE は拒否する。"""

        entity = statement.column_descriptions[0]["entity"]
        if (
            entity not in (Run, TaskSchedule, ProjectDocument)
            or statement._for_update_arg is not None
        ):
            return await super().scalars(statement)
        params = statement.compile().params
        assert set(params) == {"project_id_1"}
        assert statement.whereclause is not None and statement.whereclause.compare(
            entity.project_id == params["project_id_1"]
        )
        self.statements.append(statement)
        groups: dict[type[Any], list[Any]] = {
            Run: self.runs,
            TaskSchedule: self.schedules,
            ProjectDocument: self.documents,
        }
        rows = groups[entity]
        return [row for row in rows if row.project_id == params["project_id_1"]]

    async def execute(self, statement: Select[Any]) -> list[Any]:
        """親子どちらかが対象なら残し、outer join を inner join に変更しても通さない。"""

        assert [item["entity"] for item in statement.column_descriptions] == [
            TaskScheduleOccurrence,
            TaskSchedule,
        ]
        assert statement._for_update_arg is None
        join = statement.get_final_froms()[0]
        assert isinstance(join, Join)
        assert join.onclause is not None
        assert join.isouter and join.onclause.compare(
            TaskSchedule.id == TaskScheduleOccurrence.schedule_id
        )
        params = statement.compile().params
        assert len(params) == 2 and set(params.values()) == {self.project.id}
        assert statement.whereclause is not None and statement.whereclause.compare(
            or_(
                TaskScheduleOccurrence.project_id == self.project.id,
                TaskSchedule.project_id == self.project.id,
            )
        )
        self.statements.append(statement)
        result = []
        for row in self.occurrences:
            parent = next((item for item in self.schedules if item.id == row.schedule_id), None)
            if row.project_id == self.project.id or (
                parent is not None and parent.project_id == self.project.id
            ):
                result.append((row, parent))
        return result

    async def get(
        self, model: type[Any], identifier: UUID,
    ) -> ProjectDocument | ProjectDocumentUpload | None:
        """ORM identity map の対象を省略せず、現存する原 ID だけを返す。"""

        assert model in (ProjectDocument, ProjectDocumentUpload)
        rows = self.documents if model is ProjectDocument else self.intents
        return next((row for row in rows if row.id == identifier), None)

    async def delete(self, document: ProjectDocument) -> None:
        """DB 内の削除だけを反映し、storage に自動波及させない。"""

        assert self.in_transaction and document in self.documents
        assert any(row.document_id == document.id for row in self.cleanups)
        self.documents.remove(document)

    def add(self, row: Any) -> None:
        """新旧の清理要求を目録削除より先に保存し、原 DELETE の資格を明示する。"""

        if not isinstance(row, ProjectDocumentCleanup):
            super().add(row)
            return
        assert self.in_transaction and row.project_id == self.project.id
        assert row.organization_id == self.user.organization_id and row.requested_by == self.user.id
        assert row.request_id == self.access.request_id and row.session_id == self.auth_session.id
        assert any(document.id == row.document_id for document in self.documents)
        self.cleanups.append(row)

    async def delete_blob(self, key: str) -> None:
        """commit 応答が確定した後だけ元 key の storage I/O を許す。"""

        assert not self.in_transaction and self.commits == 1 and not self.commit_error
        assert key == self.document.storage_key

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        """原 commit の持続/非持続を親と共有し、文書 rollback も独立に確認する。"""

        before, commits = list(self.documents), self.commits
        original_cleanups = list(self.cleanups)
        original_intents = list(self.intents)
        snapshots = {
            row.id: {
                column.name: deepcopy(getattr(row, column.name))
                for column in ProjectDocumentUpload.__table__.columns
            }
            for row in original_intents
        }
        try:
            async with super().transaction() as repository:
                yield repository
        except BaseException:
            if self.commits == commits:
                self.documents = before
                self.cleanups = original_cleanups
                self.intents = original_intents
                for row in self.intents:
                    for name, value in snapshots[row.id].items():
                        setattr(row, name, value)
            raise

    async def remove_document(self) -> None:
        """必須の原 access と精確 ID で実削除 use case を呼ぶ。"""

        await self.document_service.delete_document(
            project_id=self.project.id, document_id=self.document.id, access=self.access
        )

    async def read_document(self) -> Any:
        """metadata GET が storage/変更 transaction を使わないことを検査する。"""

        return await DocumentRepository(self.session).get(
            project_id=self.project.id, document_id=self.document.id
        )
