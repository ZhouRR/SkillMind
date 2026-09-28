"""HTTP/SQL の原要求、承認、回読と未知結果の非再送を確認する。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skillmind.agent.http_source import HttpResourceError, HttpResponse
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.http_provider import HttpWriteProvider
from skillmind.effects.postgres_native import NativeDatabaseWriteProvider
from skillmind.effects.redmine import EffectProviderStaleError, EffectProviderTransportError
from tests.agent.test_mcp_tools import execution


def http_execution():
    """既存 Effect fixture を一つの明示 API 更新へ置き換える。"""
    return replace(
        execution(),
        provider="http",
        capability_version="http.write/v1",
        operation="PUT",
        target={"locator": "/items/1", "display": "item"},
        integration_config={"base_url": "https://api.example.test", "auth_mode": "none"},
        integration_scope={"paths": ["/items"], "methods": ["GET", "PUT"]},
        changes=(
            {
                "path": "/request",
                "action": "SET",
                "value": {
                    "request": {"method": "PUT", "path": "/items/1", "body": {"done": True}},
                    "read_back": {"path": "/items/1"},
                    "checks": [{"pointer": "/done", "equals": True}],
                },
            },
        ),
        precondition={"revision": "sha256:" + sha256_hex(b'{"done":false}')},
    )


async def test_http_write_uses_observed_etag_and_readback_without_replaying():
    """transport 成功だけでなく指定した JSON 条件を実 GET で確認する。"""
    source = SimpleNamespace(
        request=AsyncMock(
            side_effect=[
                HttpResponse(200, {"etag": '"v1"'}, b'{"done":false}'),
                HttpResponse(204, {}, b""),
                HttpResponse(200, {}, b'{"done":true}'),
            ]
        )
    )
    authorize = AsyncMock()
    provider = HttpWriteProvider(source=source, authorize=authorize)
    result = await provider.apply(http_execution(), credential=None)
    assert result.verification["http_status"] == 204
    assert source.request.await_args_list[1].args[2]["headers"]["If-Match"] == '"v1"'
    assert source.request.await_count == 3 and authorize.await_count == 7
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(replace(http_execution(), attempt_no=2), credential=None)
    assert source.request.await_count == 3


@pytest.mark.parametrize(
    "after_send", [HttpResourceError("transport_unconfirmed"), HttpResponse(202, {}, b"{}")]
)
async def test_http_timeout_and_async_acceptance_never_repeat_mutation(after_send):
    """送信済みかもしれない要求を反復せず、202 を完成と表示しない。"""
    source = SimpleNamespace(
        request=AsyncMock(side_effect=[HttpResponse(200, {}, b'{"done":false}'), after_send])
    )
    with pytest.raises(EffectProviderTransportError):
        await HttpWriteProvider(source=source, authorize=AsyncMock()).apply(
            http_execution(), credential=None
        )
    assert source.request.await_count == 2


async def test_http_changed_precondition_prevents_mutation():
    """古い read hash のまま上書きしない。"""
    source = SimpleNamespace(
        request=AsyncMock(return_value=HttpResponse(200, {}, b'{"done":true}'))
    )
    with pytest.raises(EffectProviderStaleError):
        await HttpWriteProvider(source=source, authorize=AsyncMock()).apply(
            http_execution(), credential=None
        )
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


@pytest.mark.parametrize("outcome", ["found", "missing", "conflict"])
async def test_native_sql_reconciliation_uses_readonly_original_receipt(monkeypatch, outcome):
    """公開された只読照会経路で元回执を確認し、不在でも SQL を再送しない。"""
    from uuid import uuid4
    from skillmind.effects import postgres_native as module
    from skillmind.effects.reconciliation_domain import EffectReconciliationInput, EffectReconciliationReference, EffectReconciliationTarget
    from skillmind.effects.reconciliation_service import EffectReconciliationService
    from skillmind.effects.reconciliation_requests import reconciliation_receipt_json
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
