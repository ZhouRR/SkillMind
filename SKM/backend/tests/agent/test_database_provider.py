"""PostgreSQL の SQL 境界、原 binding、応答と Evidence を外部 DB なしで検証する。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.context_builder import ContractStore, _read_tool_definitions
from skillmind.agent.database_provider import DatabaseReadProvider
from skillmind.agent.domain import RegisteredTool, RunWorkspace
from skillmind.agent.postgres_source import (
    MAX_DATABASE_BYTES,
    DatabaseReadError,
    DatabaseRows,
    PostgresDatabaseSource,
    build_database_query,
    database_statement,
    read_database_rows,
)
from skillmind.agent.run_binding import BoundRunResource, RunBindingError
from skillmind.agent.tool_gateway import RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.domain import (
    INSTALLED_PROVIDER_CAPABILITIES,
    IntegrationStatus,
    ResolvedIntegration,
)
from skillmind.skills.interpreter import load_capability_catalog
from skillmind.skills.manifest_gate import ManifestValidator
from skillmind.skills.resource_binding import (
    ProjectResourceCandidate,
    TaskReadinessLevel,
    evaluate_blueprint_readiness,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def resource():
    """実 I/O を持たず、同じ Project の PostgreSQL binding を提供する。"""
    integration = ResolvedIntegration(
        integration_id=uuid4(),
        project_id=uuid4(),
        name="Reports",
        kind="other",
        provider="postgres",
        status=IntegrationStatus.ACTIVE,
        revision=1,
        capabilities=("database.read/v1",),
        scope={"tables": ["public.reports"]},
        config={
            "host": "db.example.test",
            "port": 5432,
            "database": "reports",
            "username": "reader",
            "sslmode": "verify-full",
        },
        secret_reference_id=uuid4(),
    )
    return BoundRunResource(integration, integration.scope, "sha256:" + "a" * 64)


@pytest.fixture
def context(resource, tmp_path):
    """ツール自身の固定 identity を持ち、入力から書換えられない context。"""
    return RunToolContext(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        project_id=resource.integration.project_id,
        user_id=uuid4(),
        tool=RegisteredTool(
            capability="database.read/v1",
            sdk_name="mcp__skillmind__database_read_v1",
            provider="postgres",
            integration_id=resource.integration.integration_id,
            binding_id=uuid4(),
            input_schema={},
        ),
        workspace=RunWorkspace(
            tmp_path,
            tmp_path / "input",
            tmp_path / "workspace",
            tmp_path / "output",
            tmp_path / "tmp",
        ),
    )


@pytest.fixture
def provider(monkeypatch, resource):
    """DB の session だけを fake にし、Provider 本体の認可分岐を実行する。"""
    session = AsyncMock()
    factory = Mock(return_value=session)
    bound = AsyncMock(return_value=resource)
    secret = AsyncMock(return_value="fixture-only")
    monkeypatch.setattr("skillmind.agent.database_provider.load_bound_run_resource", bound)
    monkeypatch.setattr("skillmind.agent.database_provider.resolve_binding_secret", secret)
    source = AsyncMock()
    source.read.return_value = DatabaseRows(({"id": 1, "title": "Example"},), False)
    return (
        DatabaseReadProvider(factory, source=source, secret_resolver=Mock()),
        source,
        bound,
        secret,
    )


@pytest.mark.asyncio
async def test_bound_read_returns_valid_content_hash_and_evidence(provider, context):
    """Gateway が追加する参照を含め、versioned response と本文 hash が一致する。"""
    implementation, source, bound, secret = provider
    result = await implementation.execute(context, {"table": "public.reports", "purpose": "Review"})
    assert bound.await_count == 2 and secret.await_count == 2
    assert source.read.await_count == 1
    schema = ContractStore(ROOT / "contracts").load("tools/database.read/v1/response.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        {**result.response, "evidence_refs": ["ev_example"]}
    )
    content = {key: result.response[key] for key in ("table", "rows", "read_at", "truncated")}
    assert result.evidence[0].content_hash == result.response["content_hash"]
    assert result.response["content_hash"] == f"sha256:{sha256_hex(canonical_json(content))}"
    assert "fixture-only" not in str(result) and "db.example.test" not in str(result)
    for call in bound.await_args_list:
        assert call.kwargs["run_id"] == context.run_id
        assert call.kwargs["binding_id"] == context.tool.binding_id
        assert call.kwargs["capability"] == "database.read/v1"


@pytest.mark.asyncio
async def test_outside_scope_never_contacts_database(provider, context):
    """他のテーブルをモデルが要求してもネットワーク I/O を行わない。"""
    implementation, source, _, _ = provider
    with pytest.raises(ToolProviderError) as failure:
        await implementation.execute(context, {"table": "private.reports"})
    assert failure.value.code == "scope_denied"
    source.read.assert_not_called()


@pytest.mark.asyncio
async def test_revocation_after_read_discards_data(provider, context, resource):
    """接続前の成功を使って、読取中に失効した binding の結果を公開しない。"""
    implementation, source, bound, _ = provider
    bound.side_effect = [
        resource,
        RunBindingError("unavailable", "Resource revoked", retryable=False),
    ]
    with pytest.raises(ToolProviderError, match="Resource revoked"):
        await implementation.execute(context, {"table": "public.reports"})
    assert source.read.await_count == 1


@pytest.mark.asyncio
async def test_rotated_credential_after_read_discards_data(provider, context):
    """原読取が新しい credential で実行されたかのように扱わない。"""
    implementation, _, _, secret = provider
    secret.side_effect = ["fixture-only", "different-fixture"]
    with pytest.raises(ToolProviderError, match="changed during read"):
        await implementation.execute(context, {"table": "public.reports"})


@pytest.mark.asyncio
async def test_missing_binding_never_contacts_database(provider, context):
    """未束縛 Provider を本番で fixture に置き換えない。"""
    implementation, source, _, _ = provider
    with pytest.raises(ToolProviderError, match="binding is invalid"):
        await implementation.execute(
            replace(context, tool=replace(context.tool, binding_id=None)),
            {"table": "public.reports"},
        )
    source.read.assert_not_called()


@pytest.mark.asyncio
async def test_transport_failure_does_not_reflect_private_detail(provider, context):
    """Driver の詳細を公開 error に含めない。"""
    implementation, source, _, _ = provider
    source.read.side_effect = DatabaseReadError("private database diagnostic")
    with pytest.raises(ToolProviderError, match="could not be completed") as caught:
        await implementation.execute(context, {"table": "public.reports"})
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        {"table": "public.reports;DELETE FROM x"},
        {"table": "public.reports", "columns": ['x";select']},
        {"table": "public.reports", "limit": True},
        {"table": "public.reports", "offset": -1},
        {"table": "public.reports", "limit": 101},
        {"table": "public.reports", "sql": "SELECT 1"},
        {"table": "public.reports", "filters": {"id": {"operator": "raw"}}},
    ],
)
def test_request_rejects_sql_fragments_and_unbounded_reads(arguments):
    """識別子と取得量は契約と直接 Provider 呼出しの両方で制限する。"""
    with pytest.raises(ValueError):
        build_database_query(arguments)


def test_filter_values_are_bound_not_interpolated():
    """SQL らしい値は query text に含めず bind parameter として扱う。"""
    value = "x'; DELETE FROM reports; --"
    query = build_database_query(
        {
            "table": "public.reports",
            "columns": ["title"],
            "filters": {"title": value},
            "order_by": ["title"],
            "limit": 2,
        }
    )
    sql, params = database_statement(query)
    assert value not in sql and params["filter_0"] == value
    assert 'FROM "public"."reports"' in sql and 'ORDER BY "title"' in sql
    assert params["row_limit"] == 3


class RowStream:
    """Driver cursor の逐次取得を再現し、余分な行を消費しないことを観測する。"""

    def __init__(self, rows):
        """未消費行と読取数を保持する。"""
        self.rows = rows
        self.consumed = 0

    def mappings(self):
        """SQLAlchemy の mapping stream と同じ逐次 interface を返す。"""
        return self

    async def __aiter__(self):
        """一行ごとの wire 受信を数える。"""
        for row in self.rows:
            self.consumed += 1
            yield row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row", [{"payload": None}, {"payload": '{"x":"' + "a" * MAX_DATABASE_BYTES + '"}'}]
)
async def test_oversized_rows_stop_stream_without_unbounded_consumption(row):
    """SQL 側で拒否した巨大行も、累積上限超過も後続を読まない。"""
    stream = RowStream([row, {"payload": '{"id":2}'}])

    @asynccontextmanager
    async def open_stream(*args, **kwargs):
        """一回の stream を確実に閉じる context manager。"""
        yield stream

    connection = Mock(stream=open_stream)
    result = await read_database_rows(connection, build_database_query({"table": "public.reports"}))
    assert result.truncated and result.rows == () and stream.consumed == 1


@pytest.mark.asyncio
async def test_source_uses_explicit_tls_read_only_transaction_and_disposes(monkeypatch, resource):
    """外部 DB を起動せず、接続設定・transaction・最終 cleanup の実装を確認する。"""
    connection = AsyncMock()
    connection.begin = Mock(return_value=AsyncMock())
    engine = AsyncMock()
    engine.connect = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=connection)))
    factory = Mock(return_value=engine)
    monkeypatch.setattr("skillmind.agent.postgres_source.create_async_engine", factory)
    reader = AsyncMock(return_value=DatabaseRows((), False))
    monkeypatch.setattr("skillmind.agent.postgres_source.read_database_rows", reader)
    await PostgresDatabaseSource().read(
        resource.integration.config,
        "fixture-only",
        build_database_query({"table": "public.reports"}),
    )
    settings = factory.call_args.kwargs
    assert settings["hide_parameters"] is True
    assert settings["connect_args"]["ssl"] == "verify-full"
    assert settings["connect_args"]["server_settings"]["default_transaction_read_only"] == "on"
    connection.exec_driver_sql.assert_awaited_once_with(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_source_closes_connection_on_failure_or_cancellation(
    monkeypatch, resource, cancelled
):
    """失敗も取消も接続を閉じ、取消を再試行可能な読取エラーへ変換しない。"""
    connection = AsyncMock()
    transaction = AsyncMock()
    connection.begin = Mock(return_value=transaction)
    lease = AsyncMock(__aenter__=AsyncMock(return_value=connection))
    engine = AsyncMock(connect=Mock(return_value=lease))
    monkeypatch.setattr(
        "skillmind.agent.postgres_source.create_async_engine", Mock(return_value=engine)
    )
    error = asyncio.CancelledError() if cancelled else OSError("private connection detail")
    monkeypatch.setattr(
        "skillmind.agent.postgres_source.read_database_rows", AsyncMock(side_effect=error)
    )
    with pytest.raises(asyncio.CancelledError if cancelled else DatabaseReadError) as failure:
        await PostgresDatabaseSource().read(
            resource.integration.config,
            "fixture-only",
            build_database_query({"table": "public.reports"}),
        )
    assert "private connection detail" not in str(failure.value)
    transaction.__aexit__.assert_awaited_once()
    lease.__aexit__.assert_awaited_once()
    engine.dispose.assert_awaited_once()


def test_registry_only_exposes_injected_postgres_provider(provider):
    """空 registry に暗黙追加せず、Worker の注入先だけを公開する。"""
    contracts = ContractStore(ROOT / "contracts")
    assert _read_tool_definitions(contracts) == ()
    definitions = _read_tool_definitions(contracts, database_provider=provider[0])
    assert len(definitions) == 1 and set(definitions[0].providers) == {"postgres"}


def test_postgres_catalog_is_publishable_and_runnable_with_bound_resource(resource):
    """Interpreter・発行 gate・資源就緒度が同じ登録済み能力を利用できる。"""
    catalog = load_capability_catalog(ROOT / "contracts/examples/skill-capability-catalog.v1.json")
    entry = next(item for item in catalog.capabilities if item.capability == "database.read/v1")
    assert entry.providers == ("postgres",)
    contracts = ContractStore(ROOT / "contracts")
    for path in (entry.request_schema, entry.response_schema, entry.error_schema):
        Draft202012Validator.check_schema(contracts.load(path))
    registered = ManifestValidator(ROOT / "contracts").registered_capabilities
    assert entry.capability in registered
    readiness = evaluate_blueprint_readiness(
        {
            "resource_requirements": [
                {
                    "key": "reports",
                    "kind": "other",
                    "required": True,
                    "access": "read",
                    "capabilities": [entry.capability],
                    "accepted_providers": ["postgres"],
                }
            ],
        },
        candidates=[
            ProjectResourceCandidate(
                key="reports",
                kind="other",
                provider="postgres",
                label="Reports",
                capabilities=(entry.capability,),
                integration_id=resource.integration.integration_id,
                scope=resource.scope,
            )
        ],
        registered_capabilities=registered,
        installed_provider_capabilities=INSTALLED_PROVIDER_CAPABILITIES,
    )
    assert readiness.level is TaskReadinessLevel.RUNNABLE
    assert readiness.requirements[0].candidates[0].provider == "postgres"
