"""原 upload repository の公開回执、保存完全性と精確 SQL 作用域を検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select, select

from skillmind.core.hashing import sha256_hex
from skillmind.db.models import ProjectDocumentUpload, ProjectDocumentUploadClosure
from skillmind.documents.domain import (
    DocumentUploadInvalidError,
    StoredDocument,
    UploadDocumentCommand,
)
from skillmind.documents.paths import document_storage_key
from skillmind.documents.upload_intent import StoredUploadIntent
from skillmind.documents.upload_repository import DocumentUploadRepository
from skillmind.storage import BlobReference, InMemoryFileStorage


def _reservation() -> tuple[
    DocumentUploadRepository,
    MagicMock,
    ProjectDocumentUpload,
    StoredUploadIntent,
]:
    """実 reserve を通した完全な原記録を、DB に接続せず作る。"""

    session = MagicMock()
    project_id, document_id = uuid4(), uuid4()
    command = UploadDocumentCommand(
        project_id=project_id,
        document_id=document_id,
        folder="specs",
        name="note.txt",
        storage_key=document_storage_key(project_id, document_id, "note.txt"),
        storage_namespace=InMemoryFileStorage().namespace,
        upload_intent_id=uuid4(),
        size=5,
        mime="text/plain",
        checksum=f"sha256:{sha256_hex(b'hello')}",
        uploaded_by=uuid4(),
    )
    repository = DocumentUploadRepository(session)
    intent = repository.reserve(
        upload_key=uuid4(),
        organization_id=uuid4(),
        original_request_id=uuid4(),
        original_session_id=uuid4(),
        command=command,
        now=datetime.now(UTC),
    )
    row = session.add.call_args.args[0]
    assert isinstance(row, ProjectDocumentUpload)

    async def scalar(statement: Select[Any]) -> ProjectDocumentUpload | None:
        """Upload と独立 closure の行を混ぜず、未知 SELECT を黙って成功させない。"""

        entity = statement.column_descriptions[0]["entity"]
        if entity is ProjectDocumentUpload:
            return row
        assert entity is ProjectDocumentUploadClosure
        expected = (
            select(ProjectDocumentUploadClosure)
            .where(
                ProjectDocumentUploadClosure.upload_intent_id == row.id,
            )
            .execution_options(populate_existing=True)
        )
        assert statement.compare(expected)
        assert statement.get_execution_options()["populate_existing"] is True
        return None

    session.scalar = AsyncMock(side_effect=scalar)
    session.get = AsyncMock(return_value=row)
    return repository, session, row, intent


def _document(intent: StoredUploadIntent) -> StoredDocument:
    """原予約と同じ九 field を持ち、予約以後に公開された metadata を作る。"""

    command = intent.command
    return StoredDocument(
        document_id=command.document_id,
        project_id=command.project_id,
        folder=command.folder,
        name=command.name,
        size=command.size,
        mime=command.mime,
        checksum=command.checksum,
        uploaded_by=command.uploaded_by,
        created_at=intent.receipt.created_at + timedelta(seconds=1),
    )


async def _find(
    repository: DocumentUploadRepository,
    intent: StoredUploadIntent,
) -> StoredUploadIntent:
    """保存時と同じ actor/Project/key の読取だけを共通化する。"""

    result = await repository.find(
        organization_id=intent.organization_id,
        project_id=intent.command.project_id,
        actor_id=intent.actor_id,
        upload_key=intent.receipt.upload_key,
    )
    assert result is not None
    return result


async def test_find_locks_exact_organization_project_actor_and_original_key() -> None:
    """同 key を使う別 actor や Project に回执を漏らす条件省略を検出する。"""

    repository, session, row, intent = _reservation()
    assert await _find(repository, intent) == intent
    statement = session.scalar.await_args_list[0].args[0]
    expected = (
        select(ProjectDocumentUpload)
        .where(
            ProjectDocumentUpload.organization_id == row.organization_id,
            ProjectDocumentUpload.project_id == row.project_id,
            ProjectDocumentUpload.actor_id == row.actor_id,
            ProjectDocumentUpload.upload_key == row.upload_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert statement.compare(expected)
    assert statement.get_execution_options()["populate_existing"] is True
    assert statement._for_update_arg is not None
    assert session.scalar.await_count == 2
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", UUID(int=0)),
        ("document_id", UUID(int=0)),
        ("upload_key", UUID(int=0)),
        ("original_request_id", UUID(int=0)),
        ("original_session_id", UUID(int=0)),
        ("protocol_version", 2),
        ("protocol_version", True),
        ("write_protocol", "UNKNOWN"),
        ("state", "DONE"),
        ("size", 0),
        ("size", True),
        ("checksum", "sha256:invalid"),
        ("request_checksum", "sha256:" + "0" * 64),
        ("folder", " specs "),
        ("folder", "../specs"),
        ("name", "../note.txt"),
        ("name", "other.txt"),
        ("storage_key", "unrelated/note.txt"),
        ("storage_namespace_id", UUID(int=0)),
        ("storage_descriptor_checksum", "invalid"),
        ("storage_is_durable", 1),
        ("mime", "Text/Plain"),
        ("created_at", datetime(2026, 1, 1)),
        ("published_at", datetime(2026, 1, 1, tzinfo=UTC)),
        ("cleanup_requested_at", datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
async def test_corrupt_pending_record_never_becomes_a_public_receipt(
    field: str,
    value: object,
) -> None:
    """型・世代・要求摘要・正規化 path・時刻の破損を補造せず拒否する。"""

    repository, _, row, intent = _reservation()
    setattr(row, field, value)
    with pytest.raises(DocumentUploadInvalidError):
        await _find(repository, intent)


async def test_publish_and_cleanup_preserve_original_receipt_without_requiring_directory() -> None:
    """公開回执は目録を読まず再現し、清理要求で原結果や size を消去しない。"""

    repository, session, row, intent = _reservation()
    document = _document(intent)
    await repository.publish(intent, document)
    published = await _find(repository, intent)
    assert published.receipt.state == "PUBLISHED"
    assert published.receipt.document == document
    now = document.created_at + timedelta(seconds=1)
    await repository.request_cleanup(
        intent_id=intent.intent_id,
        document=document,
        reference=BlobReference(intent.command.storage_key, intent.command.storage_namespace),
        now=now,
    )
    cleaned = await _find(repository, intent)
    assert cleaned.receipt == published.receipt
    assert cleaned.cleanup_requested_at == now
    assert row.size == intent.command.size
    session.delete.assert_not_called()
    await repository.request_cleanup(
        intent_id=intent.intent_id,
        document=document,
        reference=BlobReference(intent.command.storage_key, intent.command.storage_namespace),
        now=now + timedelta(seconds=1),
    )
    assert row.cleanup_requested_at == now


@pytest.mark.parametrize("changed", ["document", "request", "published"])
async def test_publish_cannot_replace_another_request_or_change_original_metadata(
    changed: str,
) -> None:
    """現在の行が原受付から変わったとき、公開済み含め上書きしない。"""

    repository, _, row, intent = _reservation()
    document = _document(intent)
    if changed == "document":
        document = replace(document, document_id=uuid4())
    elif changed == "request":
        row.original_request_id = uuid4()
    else:
        await repository.publish(intent, document)
    with pytest.raises(DocumentUploadInvalidError):
        await repository.publish(intent, document)


@pytest.mark.parametrize("changed", ["document", "key", "namespace"])
async def test_cleanup_requires_original_metadata_and_exact_storage_reference(changed: str) -> None:
    """文書 ID・内容と原 namespace/key が違う清理要求を保存しない。"""

    repository, _, row, intent = _reservation()
    document = _document(intent)
    await repository.publish(intent, document)
    reference = BlobReference(intent.command.storage_key, intent.command.storage_namespace)
    if changed == "document":
        document = replace(document, checksum="sha256:" + "0" * 64)
    elif changed == "key":
        reference = replace(reference, key="unrelated")
    else:
        reference = replace(reference, namespace=InMemoryFileStorage().namespace)
    with pytest.raises(DocumentUploadInvalidError):
        await repository.request_cleanup(
            intent_id=intent.intent_id,
            document=document,
            reference=reference,
            now=document.created_at + timedelta(seconds=1),
        )
    assert row.cleanup_requested_at is None
