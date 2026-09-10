"""実 repository の集計 SQL を隔離 memory SQLite で実行する。PG lock の証拠ではない。"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import sqlite

from skillmind.documents.domain import DocumentUploadInvalidError
from skillmind.documents.repository import DocumentRepository


class QuotaSession:
    """既存 DB へ接続せず、生成された SUM/CASE と Project 条件を実行する。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """各 test 固有の memory connection のみを保持する。"""

        self.connection = connection

    async def scalar(self, statement: Select[Any]) -> object:
        """SQL を実行するため、fake の独自 Python 合算で誤った述語を隠さない。"""

        compiled = statement.compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True},
        )
        return self.connection.execute(str(compiled)).fetchone()[0]


def _tables(connection: sqlite3.Connection) -> None:
    """集計の関係だけを用意する。migration/FK/PG concurrency は別の回帰が担当する。"""

    connection.execute(
        "CREATE TABLE project_documents (project_id TEXT, upload_intent_id TEXT, size INTEGER)"
    )
    connection.execute(
        "CREATE TABLE document_upload_intents (project_id TEXT, size INTEGER, state TEXT)"
    )
    connection.execute(
        "CREATE TABLE document_blob_cleanups (project_id TEXT, upload_intent_id TEXT, size INTEGER)"
    )


def _document(
    connection: sqlite3.Connection, project_id: UUID, *, size: int, linked: bool,
) -> None:
    """旧目録と原予約に関連済み目録を明示して合算へ渡す。"""

    connection.execute(
        "INSERT INTO project_documents VALUES (?, ?, ?)",
        (project_id.hex, uuid4().hex if linked else None, size),
    )


@pytest.mark.parametrize("state", ["PENDING", "PUBLISHED"])
async def test_quota_charges_intents_once_and_legacy_metadata_separately(state: str) -> None:
    """原予約は公開前後で同額、関連目録は二重計上せず、別 Project は混ぜない。"""

    connection = sqlite3.connect(":memory:")
    try:
        _tables(connection)
        project_id, other = uuid4(), uuid4()
        connection.executemany(
            "INSERT INTO document_upload_intents VALUES (?, ?, ?)",
            [(project_id.hex, 10, state), (other.hex, 500, "PENDING")],
        )
        _document(connection, project_id, size=3, linked=False)
        _document(connection, other, size=-100, linked=False)
        if state == "PUBLISHED":
            _document(connection, project_id, size=10, linked=True)
        repository = DocumentRepository(QuotaSession(connection))  # type: ignore[arg-type]
        assert await repository.project_usage_bytes(project_id) == 13
        connection.execute(
            "DELETE FROM project_documents WHERE upload_intent_id IS NOT NULL"
        )
        assert await repository.project_usage_bytes(project_id) == 13
    finally:
        connection.close()


async def test_negative_legacy_size_refuses_instead_of_manufacturing_new_quota() -> None:
    """総和が正数でも一件の旧負数は空き領域の証明として使わない。"""

    connection = sqlite3.connect(":memory:")
    try:
        _tables(connection)
        project_id = uuid4()
        _document(connection, project_id, size=-1, linked=False)
        _document(connection, project_id, size=100, linked=False)
        repository = DocumentRepository(QuotaSession(connection))  # type: ignore[arg-type]
        with pytest.raises(DocumentUploadInvalidError):
            await repository.project_usage_bytes(project_id)
    finally:
        connection.close()


async def test_empty_project_has_zero_charge() -> None:
    """両方の SUM が NULL の空 Project だけは零 byte として正しく返す。"""

    connection = sqlite3.connect(":memory:")
    try:
        _tables(connection)
        repository = DocumentRepository(QuotaSession(connection))  # type: ignore[arg-type]
        assert await repository.project_usage_bytes(uuid4()) == 0
    finally:
        connection.close()


async def test_legacy_cleanup_transfers_charge_and_linked_cleanup_does_not_duplicate_it() -> None:
    """旧目録から清理行への移管は同額、新 intent と関連清理は一回分だけを数える。"""

    connection = sqlite3.connect(":memory:")
    try:
        _tables(connection)
        project_id, other = uuid4(), uuid4()
        _document(connection, project_id, size=5, linked=False)
        connection.execute(
            "INSERT INTO document_upload_intents VALUES (?, ?, ?)",
            (project_id.hex, 10, "PUBLISHED"),
        )
        repository = DocumentRepository(QuotaSession(connection))  # type: ignore[arg-type]
        assert await repository.project_usage_bytes(project_id) == 15
        connection.execute("DELETE FROM project_documents")
        connection.executemany(
            "INSERT INTO document_blob_cleanups VALUES (?, ?, ?)",
            [(project_id.hex, None, 5), (project_id.hex, uuid4().hex, 10),
             (other.hex, None, 100)],
        )
        assert await repository.project_usage_bytes(project_id) == 15
    finally:
        connection.close()


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "12"])
async def test_invalid_aggregate_result_is_not_coerced_or_assumed_zero(value: object) -> None:
    """不正集計は int 切捨てや falsy→0 で新規 upload を許可しない。"""

    session = MagicMock()
    session.scalar = AsyncMock(return_value=value)
    with pytest.raises(DocumentUploadInvalidError):
        await DocumentRepository(session).project_usage_bytes(uuid4())
