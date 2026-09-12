"""空 table の構造観測、同一 transaction、権限と旧応答互換を検証する。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from jsonschema import Draft202012Validator

from skillmind.agent.context_builder import ContractStore
from skillmind.agent.postgres_schema import validate_table_schema
from skillmind.agent.postgres_source import (
    MAX_DATABASE_BYTES,
    DatabaseReadError,
    DatabaseRows,
    PostgresDatabaseSource,
    build_database_query,
)
from skillmind.agent.run_binding import RunBindingError
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from tests.agent.test_database_provider import (
    ROOT,
)
from tests.agent.test_database_provider import (
    context as context,
)
from tests.agent.test_database_provider import (
    provider as provider,
)
from tests.agent.test_database_provider import (
    resource as resource,
)


@pytest.fixture
def schema():
    """公開した空 table 応答の構造を、各 test に独立した object で返す。"""

    return json.loads(
        (ROOT / "contracts/examples/database-read-schema-response.v1.json").read_text()
    )["table_schema"]


async def test_empty_table_schema_is_returned_and_bound_to_evidence(provider, context, schema):
    """行が無くても列/生成列/主キーを返し、同じ応答 hash に構造も含める。"""

    implementation, source, bound, _secret = provider
    source.read.return_value = DatabaseRows((), False, schema)
    result = await implementation.execute(
        context, {"table": "public.reports", "include_schema": True, "purpose": "Prepare insertion"}
    )
    assert result.response["rows"] == []
    assert result.response["table_schema"] == schema
    assert result.response["table_schema"]["columns"][2]["generated"] is True
    content = {
        key: result.response[key]
        for key in ("table", "rows", "read_at", "truncated", "table_schema")
    }
    assert result.response["content_hash"] == f"sha256:{sha256_hex(canonical_json(content))}"
    assert result.evidence[0].content_hash == result.response["content_hash"]
    assert result.evidence[0].source_locator["row_hashes"] == []
    assert bound.await_count == 2
    Draft202012Validator(
        ContractStore(ROOT / "contracts").load("tools/database.read/v1/response.schema.json")
    ).validate({**result.response, "evidence_refs": ["ev_empty_schema"]})


async def test_schema_does_not_expand_binding_scope(provider, context, schema):
    """構造要求も原 table scope の検査後にだけ I/O へ進む。"""

    implementation, source, _bound, _secret = provider
    source.read.return_value = DatabaseRows((), False, schema)
    with pytest.raises(ToolProviderError) as caught:
        await implementation.execute(context, {"table": "private.reports", "include_schema": True})
    assert caught.value.code == "scope_denied"
    source.read.assert_not_awaited()


async def test_schema_is_discarded_after_revocation(provider, context, resource, schema):
    """列情報も読取後の撤権で破棄し、行が無いことを例外にしない。"""

    implementation, source, bound, _secret = provider
    source.read.return_value = DatabaseRows((), False, schema)
    bound.side_effect = [resource, RunBindingError("unavailable", "Revoked", retryable=False)]
    with pytest.raises(ToolProviderError, match="Revoked"):
        await implementation.execute(context, {"table": "public.reports", "include_schema": True})


async def test_schema_and_rows_cannot_exceed_the_combined_provider_limit(provider, context, schema):
    """個々に収まる行と構造を足して上限を超えた injected result も拒否する。"""

    implementation, source, _bound, _secret = provider
    source.read.return_value = DatabaseRows(
        ({"value": "x" * (MAX_DATABASE_BYTES - 100)},), False, schema
    )
    with pytest.raises(ToolProviderError, match="exceeds the read limit"):
        await implementation.execute(context, {"table": "public.reports", "include_schema": True})


@pytest.mark.parametrize("include", [None, False])
async def test_old_read_omits_unrequested_schema_and_preserves_hash(
    provider, context, schema, include
):
    """旧呼出は injected source が構造を返しても公開せず、旧本文の hash を維持する。"""

    implementation, source, _bound, _secret = provider
    source.read.return_value = DatabaseRows((), False, schema)
    arguments = {"table": "public.reports"}
    if include is not None:
        arguments["include_schema"] = include
    result = await implementation.execute(context, arguments)
    assert "table_schema" not in result.response
    content = {key: result.response[key] for key in ("table", "rows", "read_at", "truncated")}
    assert result.response["content_hash"] == f"sha256:{sha256_hex(canonical_json(content))}"


@pytest.mark.parametrize("value", [None, {}, {"columns": [], "primary_key": []}])
async def test_required_schema_failure_cannot_be_reported_as_empty_success(
    provider, context, value
):
    """構造取得に対応しない source や部分結果は、行が空でも成功に降格しない。"""

    implementation, source, _bound, _secret = provider
    source.read.return_value = DatabaseRows((), False, value)
    with pytest.raises(ToolProviderError, match="schema could not be confirmed"):
        await implementation.execute(context, {"table": "public.reports", "include_schema": True})


@pytest.mark.parametrize("value", [None, 1, "true", [], {}])
def test_schema_flag_has_the_same_strict_boolean_contract_at_direct_entry(value):
    """Gateway を経由しない Provider 呼出しでも truthy 値を flag にしない。"""

    arguments = {"table": "public.reports", "purpose": "Inspect", "include_schema": value}
    with pytest.raises(ValueError):
        build_database_query(arguments)
    schema = ContractStore(ROOT / "contracts").load("tools/database.read/v1/request.schema.json")
    assert not Draft202012Validator(schema).is_valid(arguments)


@pytest.mark.parametrize(
    "mutation", ["duplicate", "missing-key", "duplicate-key", "extra", "type", "wide", "bytes"]
)
def test_schema_is_complete_bounded_and_does_not_include_default_expressions(schema, mutation):
    """部分列、偽主キー、既定値本文や過大な構造を公開しない。"""

    if mutation == "duplicate":
        schema["columns"].append(deepcopy(schema["columns"][0]))
    elif mutation == "missing-key":
        schema["primary_key"] = ["missing"]
    elif mutation == "duplicate-key":
        schema["primary_key"] = ["id", "id"]
    elif mutation == "extra":
        schema["columns"][0]["default_expression"] = "private expression"
    elif mutation == "type":
        schema["columns"][0]["generated"] = 1
    else:
        schema["columns"] = [
            {**schema["columns"][0], "name": f"column_{index}", "data_type": "x" * 1000}
            for index in range(101 if mutation == "wide" else 100)
        ]
        schema["primary_key"] = []
    with pytest.raises(ValueError):
        validate_table_schema(schema)


async def test_schema_and_rows_share_one_read_only_transaction_and_byte_budget(
    monkeypatch, resource, schema
):
    """DDL を抑止する読取 lock→列→行の順序を検証する。実競争は別途受入で扱う。"""

    events = []
    connection = AsyncMock()
    connection.begin = Mock(return_value=AsyncMock())
    connection.exec_driver_sql.side_effect = lambda sql: events.append(sql)
    engine = AsyncMock()
    engine.connect = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=connection)))
    monkeypatch.setattr(
        "skillmind.agent.postgres_source.create_database_engine", Mock(return_value=engine)
    )

    async def describe(*args, **kwargs):
        """同じ接続を観測し、構造読取が行より先であることを記録する。"""

        assert args == (connection,) and kwargs == {"quoted_table": '"public"."reports"'}
        events.append("schema")
        return schema

    async def read(*args, **kwargs):
        """構造 byte を含む共通上限が行 stream へ渡るか確認する。"""

        assert args[0] is connection
        assert kwargs["byte_limit"] == MAX_DATABASE_BYTES - len(canonical_json(schema).encode())
        events.append("rows")
        return DatabaseRows((), False)

    monkeypatch.setattr("skillmind.agent.postgres_source.read_table_schema", describe)
    monkeypatch.setattr("skillmind.agent.postgres_source.read_database_rows", read)
    result = await PostgresDatabaseSource().read(
        resource.integration.config,
        "fixture-only",
        build_database_query({"table": "public.reports", "include_schema": True}),
    )
    assert result.table_schema == schema
    assert events == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        'LOCK TABLE "public"."reports" IN ACCESS SHARE MODE',
        "schema",
        "rows",
    ]
    connection.begin.assert_called_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_schema_failure_stops_before_rows_and_closes_connection(
    monkeypatch, resource, cancelled
):
    """構造が欠落/過大/読取不能なら通常 SELECT に fallback せず rollback する。"""

    connection = AsyncMock()
    transaction = AsyncMock()
    connection.begin = Mock(return_value=transaction)
    lease = AsyncMock(__aenter__=AsyncMock(return_value=connection))
    engine = AsyncMock(connect=Mock(return_value=lease))
    monkeypatch.setattr(
        "skillmind.agent.postgres_source.create_database_engine", Mock(return_value=engine)
    )
    monkeypatch.setattr(
        "skillmind.agent.postgres_source.read_table_schema",
        AsyncMock(side_effect=asyncio.CancelledError() if cancelled else ValueError("private")),
    )
    read = AsyncMock()
    monkeypatch.setattr("skillmind.agent.postgres_source.read_database_rows", read)
    with pytest.raises(asyncio.CancelledError if cancelled else DatabaseReadError):
        await PostgresDatabaseSource().read(
            resource.integration.config,
            "fixture-only",
            build_database_query({"table": "public.reports", "include_schema": True}),
        )
    read.assert_not_awaited()
    transaction.__aexit__.assert_awaited_once()
    lease.__aexit__.assert_awaited_once()
    engine.dispose.assert_awaited_once()
