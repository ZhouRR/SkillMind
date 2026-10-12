"""HTTP/SQL の原要求、承認、回読と未知結果の非再送を確認する。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skillmind.agent.http_source import HttpResourceError, HttpResponse
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.http_provider import HttpWriteProvider
from skillmind.effects.postgres_native import (
    NativeDatabaseWriteProvider,
    SqlPreconditionNotMetError,
)
from skillmind.effects.redmine import EffectProviderStaleError, EffectProviderTransportError
from tests.agent.test_mcp_tools import execution


def http_execution():
    """事前 GET を持たない承認済み HTTP request を固定する。"""
    return replace(
        execution(),
        provider="http",
        capability_version="http.write/v1",
        operation="POST",
        target={"locator": "/messages", "display": "send message"},
        integration_config={"auth_mode": "none"},
        integration_scope={"base_urls": ["https://api.example.test"]},
        changes=(
            {
                "path": "/request",
                "action": "SET",
                "value": {
                    "request": {
                        "method": "POST",
                        "path": "/messages",
                        "body": {"message": "hello"},
                    },
                },
            },
        ),
        precondition={},
        verification={"method": "HTTP_RESPONSE", "paths": ["/request"]},
    )


def http_provider(tmp_path, source, authorize=None):
    """実 file 保存を含む Provider を隔離 workspace で作る。"""
    from skillmind.agent.workspace import WorkspaceManager

    return HttpWriteProvider(
        source=source,
        authorize=authorize or AsyncMock(),
        workspace_manager=WorkspaceManager(tmp_path),
    )


@pytest.mark.parametrize("status", [200, 201, 202, 204])
async def test_http_write_returns_exact_response_without_get_or_replay(tmp_path, status):
    """受理を含む 2xx は原応答を保存し、空 body でも架空 GET を加えない。"""
    body = b"" if status == 204 else b'{"accepted":true}'
    source = SimpleNamespace(request=AsyncMock(return_value=HttpResponse(status, {}, body)))
    authorize = AsyncMock()
    provider = http_provider(tmp_path, source, authorize)
    original = http_execution()
    result = await provider.apply(original, credential=None)
    assert result.verification["method"] == "HTTP_RESPONSE"
    assert result.verification["http_status"] == status
    assert result.verification["business_verdict"] == "NOT_EVALUATED"
    assert (
        tmp_path / str(original.run_id) / result.after.content["file"]["path"]
    ).read_bytes() == body
    assert source.request.await_count == 1 and authorize.await_count == 3
    assert source.request.await_args.kwargs["write"] is True
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(replace(original, attempt_no=2), credential=None)
    assert source.request.await_count == 1


async def test_http_timeout_never_repeats_mutation(tmp_path):
    """送信済みかもしれない要求を GET や mutation で探らない。"""
    source = SimpleNamespace(
        request=AsyncMock(side_effect=HttpResourceError("transport_unconfirmed"))
    )
    with pytest.raises(EffectProviderTransportError, match="http_result_unconfirmed"):
        await http_provider(tmp_path, source).apply(http_execution(), credential=None)
    assert source.request.await_count == 1


@pytest.mark.parametrize("status", [302, 400, 429, 500])
async def test_http_error_keeps_response_and_is_not_an_unknown_result(tmp_path, status):
    """非 2xx でも正文を Agent に残し、失敗を未送信や不明へ変換しない。"""
    from skillmind.effects.http_provider import HttpResponseError
    from skillmind.effects.outcomes import effect_failure_record

    body = b'{"error":"request rejected"}'
    source = SimpleNamespace(request=AsyncMock(return_value=HttpResponse(status, {}, body)))
    original = http_execution()
    with pytest.raises(HttpResponseError) as caught:
        await http_provider(tmp_path, source).apply(original, credential=None)
    observation = caught.value.observations[0]
    assert observation.content["http_status"] == status
    assert (
        tmp_path / str(original.run_id) / observation.content["file"]["path"]
    ).read_bytes() == body
    record = effect_failure_record(
        capability=original.capability_version,
        provider="http",
        attempt_no=1,
        code=caught.value.code,
        retryable=False,
        previous=None,
    )
    assert record == {"code": "http_response_error", "retryable": False}
    assert source.request.await_count == 1


async def test_http_revocation_prevents_response_file_publication(tmp_path):
    """送信後の撤権でも再送せず、正文 file は公開しない。"""
    source = SimpleNamespace(request=AsyncMock(return_value=HttpResponse(200, {}, b"private")))
    authorize = AsyncMock(side_effect=[None, None, PermissionError("revoked")])
    original = http_execution()
    with pytest.raises(PermissionError):
        await http_provider(tmp_path, source, authorize).apply(original, credential=None)
    assert not list((tmp_path / str(original.run_id)).rglob("response.bin"))
    assert source.request.await_count == 1

class Connection:
    """transaction exit と原 receipt の SQL を記録する有界替身。"""

    def __init__(self, receipt=None):
        self.receipt, self.saved, self.exits = receipt, None, []
        self.commands = []
        self.driver = SimpleNamespace(execute=AsyncMock())

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, value, traceback):
        self.exits.append(kind)

    def begin(self):
        return self

    async def exec_driver_sql(self, sql):
        self.commands.append(sql)

    async def get_raw_connection(self):
        return SimpleNamespace(driver_connection=self.driver)

    async def execute(self, sql, params):
        value = str(sql)
        self.commands.append(value)
        if value.startswith("INSERT INTO skillmind_effects"):
            self.saved = params
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: self.receipt))


def sql_execution():
    """最小 UPDATE と変更前/後の同じ SELECT を固定する。"""
    before = {"rows": [{"done": False}], "truncated": False}
    return replace(
        execution(),
        provider="postgres",
        capability_version="database.execute/v1",
        operation="UPDATE",
        integration_config={"access_mode": "native_sql"},
        integration_scope={"statements": ["SELECT", "UPDATE"]},
        target={"locator": "database", "display": "database"},
        changes=(
            {
                "path": "/statement",
                "action": "SET",
                "value": {
                    "statement": {
                        "sql": "UPDATE public.items SET done=$1 WHERE id=$2",
                        "parameters": [True, 1],
                    },
                    "read_back": {
                        "sql": "SELECT done FROM public.items WHERE id=$1",
                        "parameters": [1],
                    },
                    "checks": [{"pointer": "/rows/0/done", "equals": True}],
                },
            },
        ),
        precondition={"revision": "sha256:" + sha256_hex(canonical_json(before))},
    )


@pytest.mark.parametrize("matches", [True, False])
async def test_sql_verification_and_receipt_share_transaction(monkeypatch, matches):
    """必要な確認に失敗した UPDATE は transaction ごと rollback する。"""
    from skillmind.effects import postgres_native as module

    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection, dispose=AsyncMock())
    monkeypatch.setattr(module, "create_database_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(
        module,
        "query_rows",
        AsyncMock(
            side_effect=[
                {"rows": [{"done": False}], "truncated": False},
                {"rows": [{"done": matches}], "truncated": False},
            ]
        ),
    )
    provider = NativeDatabaseWriteProvider(AsyncMock())
    if matches:
        result = await provider.apply(sql_execution(), credential="fixture-password")
        assert not result.replayed and connection.saved is not None
        assert connection.exits == [None, None]
    else:
        with pytest.raises(EffectProviderStaleError):
            await provider.apply(sql_execution(), credential="fixture-password")
        assert connection.saved is None and all(
            kind is EffectProviderStaleError for kind in connection.exits
        )
    assert connection.driver.execute.await_count == 1
    assert "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE" in connection.commands


async def test_sql_unknown_reclaim_without_original_receipt_does_not_execute(monkeypatch):
    """不在の回执を、新しい SQL write の許可に読み替えない。"""
    from skillmind.effects import postgres_native as module

    connection = Connection()
    monkeypatch.setattr(
        module,
        "create_database_engine",
        lambda *a, **kw: SimpleNamespace(connect=lambda: connection, dispose=AsyncMock()),
    )
    read = AsyncMock()
    monkeypatch.setattr(module, "query_rows", read)
    with pytest.raises(EffectProviderTransportError):
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            replace(sql_execution(), attempt_no=2), credential="fixture-password"
        )
    read.assert_not_awaited()
    connection.driver.execute.assert_not_awaited()


@pytest.mark.parametrize("failure", ["changed", "truncated", "rollback", "revoke", "cancel"])
async def test_sql_precondition_only_proves_unsent_after_successful_cleanup(monkeypatch, failure):
    """DML 前の不一致だけを修正可能とし、rollback/撤権/取消失敗は通常保護へ渡す。"""
    import asyncio

    from skillmind.effects import postgres_native as module

    connection = Connection()
    if failure == "rollback":

        async def exit_failure(self, kind, value, traceback):
            """transaction rollback の接続エラーを模擬する。"""
            raise OSError("fixture rollback unavailable")

        monkeypatch.setattr(Connection, "__aexit__", exit_failure)
    monkeypatch.setattr(
        module,
        "create_database_engine",
        lambda *a, **kw: SimpleNamespace(connect=lambda: connection, dispose=AsyncMock()),
    )
    monkeypatch.setattr(
        module,
        "query_rows",
        AsyncMock(
            return_value={
                "rows": [{"done": True}],
                "truncated": failure == "truncated",
            }
        ),
    )
    authorize = AsyncMock()
    if failure == "revoke":
        authorize.side_effect = [None, PermissionError("revoked")]
    if failure == "cancel":
        authorize.side_effect = [None, asyncio.CancelledError()]
    expected = {
        "rollback": EffectProviderTransportError,
        "revoke": PermissionError,
        "cancel": asyncio.CancelledError,
    }.get(failure, SqlPreconditionNotMetError)
    with pytest.raises(expected):
        await NativeDatabaseWriteProvider(authorize).apply(
            sql_execution(), credential="fixture-password"
        )
    connection.driver.execute.assert_not_awaited()
    assert connection.saved is None


async def test_sql_existing_receipt_conflict_is_never_marked_unsent(monkeypatch):
    """回执がある原操作は、初回 attempt でも未送信の訂正経路へ戻さない。"""
    from skillmind.effects import postgres_native as module

    connection = Connection({"request_checksum": "different"})
    monkeypatch.setattr(
        module,
        "create_database_engine",
        lambda *a, **kw: SimpleNamespace(connect=lambda: connection, dispose=AsyncMock()),
    )
    with pytest.raises(EffectProviderStaleError) as error:
        await NativeDatabaseWriteProvider(AsyncMock()).apply(
            sql_execution(), credential="fixture-password"
        )
    assert not isinstance(error.value, SqlPreconditionNotMetError)
    connection.driver.execute.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["found", "missing", "conflict"])
async def test_native_sql_reconciliation_uses_readonly_original_receipt(monkeypatch, outcome):
    """公開された只読照会経路で元回执を確認し、不在でも SQL を再送しない。"""
    from uuid import uuid4

    from skillmind.effects import postgres_native as module
    from skillmind.effects.reconciliation_domain import (
        EffectReconciliationInput,
        EffectReconciliationReference,
        EffectReconciliationTarget,
    )
    from skillmind.effects.reconciliation_requests import reconciliation_receipt_json
    from skillmind.effects.reconciliation_service import EffectReconciliationService
    original = sql_execution()
    payload = original.changes[0]["value"]
    command = module.NativeSqlReceiptCommand(original.effect_execution_id, original.project_id, original.run_id, original.integration_id,
        module.native_request_checksum(original.run_id, original.integration_id, payload, original.precondition),
        original.precondition["revision"], canonical_json(payload["checks"]))
    stored = {"request_checksum": command.request_checksum,
              "before_row": canonical_json({"rows": [{"done": False}], "truncated": False}),
              "after_row": canonical_json({"rows": [{"done": True}], "truncated": False})}
    if outcome == "conflict": stored["request_checksum"] = "other"
    connection = Connection(None if outcome == "missing" else stored)
    factory_calls = []
    def factory(*args, **kwargs):
        factory_calls.append(kwargs)
        return SimpleNamespace(connect=lambda: connection, dispose=AsyncMock())
    monkeypatch.setattr(module, "create_database_engine", factory)
    target = EffectReconciliationTarget(uuid4(), uuid4(), "sha256:" + "a" * 64, "postgres",
        module.SQL_VERSION, command, "{}", uuid4())
    service = EffectReconciliationService(AsyncMock(), secret_resolver=AsyncMock(),
        database_reader=AsyncMock(), document_reader=None, document_library_target=None)
    service._load = AsyncMock(return_value=EffectReconciliationInput(target, "fixture-password"))
    reference = EffectReconciliationReference(uuid4(), uuid4(), uuid4(), original.project_id,
        original.run_id, original.effect_execution_id)
    result = await service.observe(reference)
    assert result.kind == "DATABASE_TRANSACTION"
    assert result.status == {"found": "CONFIRMED", "missing": "NOT_OBSERVED", "conflict": "CONFLICT"}[outcome]
    assert (reconciliation_receipt_json(result, target) is not None) is (outcome == "found")
    assert factory_calls == [{"read_only": True}]
    assert "SET TRANSACTION READ ONLY" in connection.commands
    connection.driver.execute.assert_not_awaited()
    assert connection.saved is None
