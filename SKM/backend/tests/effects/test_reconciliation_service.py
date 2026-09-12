"""照会 service と実 SQL/HTTP client を接続し、DB/HTTP 応答と権限 port だけを合成する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from skillmind.core.hashing import canonical_json
from skillmind.effects.postgres_write import PostgresDatabaseWriteSource
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationDeniedError,
    EffectReconciliationInput,
    EffectReconciliationReference,
    EffectReconciliationTarget,
    EffectReconciliationUnavailableError,
)
from skillmind.effects.reconciliation_service import EffectReconciliationService
from tests.effects.test_database_write import Connection, command, rows
from tests.storage.test_object_effect import Stream, fixture, response


def service(target, *, database_reader=None, document_reader=None):
    """参照/認可 port は別回帰へ分け、実 observe が apply を呼べない構成にする。"""
    result = EffectReconciliationService(
        MagicMock(),
        secret_resolver=MagicMock(),
        database_reader=database_reader or AsyncMock(),
        document_reader=document_reader,
        document_library_target=None,
    )
    loaded = EffectReconciliationInput(target, "fixture-secret" if database_reader else None)
    result._load = AsyncMock(return_value=loaded)
    reference = EffectReconciliationReference(
        uuid4(),
        uuid4(),
        uuid4(),
        target.command.project_id,
        target.command.run_id,
        target.command.effect_id,
    )
    return result, reference


@pytest.mark.parametrize("outcome", ["found", "missing", "conflict", "unavailable"])
async def test_postgres_query_uses_only_original_receipt_select(monkeypatch, outcome):
    """同じ原 identity を READ ONLY で照会し、不在/不明でも業務 SQL や apply へ退避しない。"""
    original = command()
    receipt = {
        "request_checksum": original.checksum,
        "before": "null",
        "after": canonical_json({**original.key, **original.values}),
    }
    if outcome == "conflict":
        receipt["request_checksum"] = "other"
    db = Connection(
        [OSError("private endpoint")]
        if outcome == "unavailable"
        else [
            rows(None if outcome == "missing" else receipt),
        ]
    )
    engine = AsyncMock()
    engine.connect = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=db)))
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr("skillmind.effects.postgres_write.create_database_engine", factory)
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "postgres",
        "postgres-receipt/v1",
        original,
        "{}",
        uuid4(),
    )
    reader, reference = service(target, database_reader=PostgresDatabaseWriteSource())
    if outcome == "unavailable":
        with pytest.raises(EffectReconciliationUnavailableError) as raised:
            await reader.observe(reference)
        assert "private" not in str(raised.value)
    else:
        observed = await reader.observe(reference)
        assert (
            observed.status
            == {
                "found": "CONFIRMED",
                "missing": "NOT_OBSERVED",
                "conflict": "CONFLICT",
            }[outcome]
        )
        assert observed.kind == "DATABASE_TRANSACTION"
        assert observed.effect_execution_id == original.effect_id
        assert (observed.receipt is not None) == (outcome == "found")
    assert factory.call_args.kwargs["read_only"] is True
    assert db.exec_driver_sql.await_args_list[0].args == ("SET TRANSACTION READ ONLY",)
    assert db.execute.await_count == 1
    sql, params = db.execute.call_args.args
    assert str(sql).startswith("SELECT request_checksum")
    assert params["effect_id"] == original.effect_id
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["found", "missing", "conflict", "unavailable"])
async def test_object_query_uses_get_and_never_publishes_or_sends(outcome):
    """原 object の byte/metadata を実 client で核対し、GET 未検出を PUT 許可にしない。"""
    calls = []

    def handle(request):
        """合成応答のみを返し、署名済み request の method を観測する。"""
        calls.append(request.method)
        assert request.method == "GET"
        if outcome == "missing":
            return httpx.Response(404, stream=Stream([b"<Error><Code>NoSuchKey</Code></Error>"]))
        if outcome == "unavailable":
            raise httpx.ReadError("private response")
        return response(original, body=b"changed" if outcome == "conflict" else None)

    source, original, _ = fixture(handler=handle)
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "project-library",
        "project-library-receipt/v1",
        original,
        "{}",
        None,
    )
    reader, reference = service(target, document_reader=source)
    if outcome == "unavailable":
        with pytest.raises(EffectReconciliationUnavailableError):
            await reader.observe(reference)
    else:
        observed = await reader.observe(reference)
        assert (
            observed.status
            == {
                "found": "CONFIRMED",
                "missing": "NOT_OBSERVED",
                "conflict": "CONFLICT",
            }[outcome]
        )
        assert observed.kind == "DOCUMENT_OBJECT"
        assert observed.request_checksum == original.request_checksum
    assert calls == ["GET"]
    reader._database_reader.lookup.assert_not_called()


@pytest.mark.parametrize("mutation", ["credential", "target", "revoked"])
async def test_changed_authority_after_read_withholds_observation(mutation):
    """I/O 後に会話/資格/元 target が変わった場合、観測を確認済みとして返さない。"""
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "postgres",
        "postgres-receipt/v1",
        command(),
        "{}",
        uuid4(),
    )
    db = AsyncMock()
    db.lookup.return_value = None
    reader, reference = service(target, database_reader=db)
    original = reader._load.return_value
    changed = (
        EffectReconciliationDeniedError("revoked")
        if mutation == "revoked"
        else replace(original, credential="new-secret")
        if mutation == "credential"
        else replace(original, target=replace(target, config_json='{"changed":true}'))
    )
    reader._load.side_effect = [original, changed]
    with pytest.raises(EffectReconciliationDeniedError):
        await reader.observe(reference)
    db.lookup.assert_awaited_once()
    db.apply.assert_not_called()


async def test_cancelled_read_waits_for_owned_cleanup_without_result():
    """上位 Worker の停止を伝播し、元 Effect の成功/失敗を作らない。"""
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def lookup(*args, **kwargs):
        """待機中の read coroutine の清理完了を記録する。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    db = AsyncMock()
    db.lookup.side_effect = lookup
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "postgres",
        "postgres-receipt/v1",
        command(),
        "{}",
        uuid4(),
    )
    reader, reference = service(target, database_reader=db)
    task = asyncio.create_task(reader.observe(reference))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_read_deadline_does_not_return_absence_or_leave_owned_lookup_running(monkeypatch):
    """30 秒期限の取消経路を短い時計で検証し、timeout を未検出へ降格しない。"""
    original_timeout = asyncio.timeout

    def timeout(seconds):
        """本 use case の期限だけを短縮し、nested client の timeout は変更しない。"""
        return original_timeout(0.03 if seconds == 30 else seconds)

    monkeypatch.setattr(asyncio, "timeout", timeout)
    cleaned = asyncio.Event()

    async def lookup(*args, **kwargs):
        """所有する読取 task の清理完了を timeout 後にも観測する。"""
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    db = AsyncMock()
    db.lookup.side_effect = lookup
    target = EffectReconciliationTarget(
        uuid4(),
        uuid4(),
        "sha256:" + "1" * 64,
        "postgres",
        "postgres-receipt/v1",
        command(),
        "{}",
        uuid4(),
    )
    reader, reference = service(target, database_reader=db)
    with pytest.raises(EffectReconciliationUnavailableError):
        await reader.observe(reference)
    assert cleaned.is_set()
    db.apply.assert_not_called()
