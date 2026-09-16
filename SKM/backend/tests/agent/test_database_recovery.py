"""構造化診断・純構造取得・再認可・明示訂正の境界を外部 I/O なしで検証する。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError

from skillmind.agent.database_errors import classify_database_error, safe_database_diagnostic
from skillmind.agent.database_observations import DatabaseObservations
from skillmind.agent.postgres_source import DatabaseReadError, DatabaseRows, PostgresDatabaseSource
from skillmind.agent.run_binding import RunBindingError
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from tests.agent.test_database_provider import ROOT
from tests.agent.test_database_provider import context as context
from tests.agent.test_database_provider import provider as provider
from tests.agent.test_database_provider import resource as resource


@pytest.fixture
def modern(context):
    """新 Run の固定 policy と既存 binding で v2 を使用する。"""
    return replace(
        context,
        tool=replace(context.tool, capability="database.read/v2"),
        run=SimpleNamespace(task_brief={"runtime_policy": "skillmind.runtime/v2"}),
        tool_call_id=uuid4(),
    )


@pytest.fixture
def schema():
    """順序付き複合主鍵と生成列を含む構造。"""
    value = json.loads(
        (ROOT / "contracts/examples/database-read-schema-response.v1.json").read_text()
    )["table_schema"]
    value["primary_key"] = ["status", "id"]
    return value


@pytest.mark.parametrize(
    "error,stage,reason,code",
    [
        (
            asyncpg.UndefinedColumnError("secret SQL"),
            "row_read",
            "undefined_column",
            "invalid_request",
        ),
        (asyncpg.UndefinedColumnError("secret SQL"), "schema_read", "read_failed", "unavailable"),
        (asyncpg.UndefinedTableError("secret SQL"), "row_read", "undefined_table", "not_found"),
        (asyncpg.UndefinedTableError("secret SQL"), "schema_read", "read_failed", "unavailable"),
        (
            asyncpg.InsufficientPrivilegeError("secret SQL"),
            "row_read",
            "insufficient_privilege",
            "scope_denied",
        ),
        (
            asyncpg.InvalidTextRepresentationError("secret value"),
            "row_read",
            "parameter_type_mismatch",
            "invalid_request",
        ),
        (
            asyncpg.ConnectionDoesNotExistError("secret host"),
            "connect",
            "connection_failure",
            "unavailable",
        ),
        (asyncpg.QueryCanceledError("secret SQL"), "row_read", "read_failed", "unavailable"),
        (ValueError("42703 undefined column password"), "row_read", "read_failed", "unavailable"),
    ],
)
def test_sqlstate_not_driver_text(error, stage, reason, code):
    """実 driver と adapter の属性を検証し、本文には依存しない。"""
    adapter = AsyncAdapt_asyncpg_dbapi.ProgrammingError("hidden adapter SQL")
    adapter.sqlstate = getattr(error, "sqlstate", None)
    adapter.__cause__ = error
    diagnostic = classify_database_error(DBAPIError("SELECT private", {}, adapter), stage)
    assert (diagnostic.reason_code, diagnostic.code) == (reason, code)
    assert "secret" not in diagnostic.message and "SELECT" not in diagnostic.message


def test_diagnostic_allowlist():
    """任意 SQL・不正 SQLSTATE を観測へ透過させない。"""
    assert safe_database_diagnostic({"reason_code": "secret", "stage": "row_read"}) is None
    value = {"reason_code": "read_failed", "stage": "row_read", "sqlstate": None}
    assert safe_database_diagnostic({**value, "sql": "secret"}) == value
    assert safe_database_diagnostic({**value, "sqlstate": "password"}) is None


@pytest.mark.asyncio
async def test_correctable_failure_and_reauthorization(provider, modern, resource):
    """誤フィルタを変更せず、撤権時は認可結果を優先する。"""
    impl, source, binding, _ = provider
    source.read.side_effect = DatabaseReadError(
        diagnostic=classify_database_error(asyncpg.UndefinedColumnError("secret"), "row_read")
    )
    with pytest.raises(ToolProviderError) as failure:
        await impl.execute(modern, {"table": "public.reports", "filters": {"wrong_key": "private"}})
    assert failure.value.code == "invalid_request" and not failure.value.retryable
    assert source.read.call_args.args[2].filters == {"wrong_key": "private"}
    assert binding.await_count == 2
    binding.side_effect = [resource, RunBindingError("scope_denied", "Revoked", retryable=False)]
    with pytest.raises(ToolProviderError, match="Revoked"):
        await impl.execute(modern, {"table": "public.reports"})


@pytest.mark.asyncio
async def test_describe_cache_refresh_empty_and_composite_key(provider, modern, schema):
    """cache hit は元時刻を保ち、DDL refresh も行 SQL は使わない。"""
    impl, source, _, _ = provider
    ctx = replace(modern, tool=replace(modern.tool, capability="database.describe/v1"))
    impl._observations.lookup = AsyncMock(return_value=None)
    source.describe.return_value = DatabaseRows((), False, schema)
    first = await impl.execute(ctx, {"table": "public.reports", "purpose": "Inspect"})
    assert first.response["table_schema"]["primary_key"] == ["status", "id"]
    assert "rows" not in first.response
    observation = {
        key: first.response[key] for key in ("table_schema", "schema_checksum", "observed_at")
    }
    observation["observation_refs"] = ["ev_original"]
    impl._observations.lookup.return_value = observation
    cached = await impl.execute(ctx, {"table": "public.reports", "purpose": "Inspect"})
    assert cached.response["observed_at"] == first.response["observed_at"]
    assert cached.response["observation_refs"] == ["ev_original"] and cached.response["cached"]
    schema["primary_key"] = []
    fresh = await impl.execute(
        ctx, {"table": "public.reports", "purpose": "DDL changed", "refresh": True}
    )
    assert fresh.response["table_schema"]["primary_key"] == [] and not fresh.response["cached"]
    assert source.describe.await_count == 2
    source.read.assert_not_called()


@pytest.mark.asyncio
async def test_source_describe_and_cancel_cleanup(monkeypatch, resource, schema):
    """Source 経路で pure schema と cancellation 優先を検証する。"""
    connection = AsyncMock()
    connection.begin = Mock(return_value=AsyncMock())
    engine = AsyncMock(
        connect=Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=connection)))
    )
    monkeypatch.setattr(
        "skillmind.agent.postgres_source.create_database_engine", Mock(return_value=engine)
    )
    reflection, rows = AsyncMock(return_value=schema), AsyncMock()
    monkeypatch.setattr("skillmind.agent.postgres_source.read_table_schema", reflection)
    monkeypatch.setattr("skillmind.agent.postgres_source.read_database_rows", rows)
    result = await PostgresDatabaseSource().describe(
        resource.integration.config, "fixture", "public.reports"
    )
    assert result.table_schema == schema
    rows.assert_not_called()
    reflection.side_effect = asyncio.CancelledError()
    engine.dispose.side_effect = OSError("private cleanup failure")
    with pytest.raises(asyncio.CancelledError):
        await PostgresDatabaseSource().describe(
            resource.integration.config, "fixture", "public.reports"
        )


@pytest.mark.asyncio
async def test_cache_binding_integrity_and_outage(modern, resource, schema):
    """同 binding/checksum の証拠のみ再利用し、cache 故障は miss、取消は伝播する。"""
    checksum = "sha256:" + sha256_hex(canonical_json(schema))
    evidence = SimpleNamespace(
        metadata_json={
            "binding_id": str(modern.tool.binding_id),
            "binding_checksum": resource.checksum,
        },
        evidence_ref="ev_original",
        content_hash=checksum,
    )
    call = SimpleNamespace(
        result_json={
            "status": "success",
            "table": "public.reports",
            "table_schema": schema,
            "schema_checksum": checksum,
            "observed_at": "2026-09-16T10:00:00+00:00",
        },
        capability_version="database.describe/v1",
    )
    session = AsyncMock()
    session.execute.return_value = Mock(all=Mock(return_value=[(evidence, call)]))
    store = DatabaseObservations(
        Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session)))
    )
    assert (await store.lookup(modern, resource, "public.reports"))["observation_refs"] == [
        "ev_original"
    ]
    evidence.metadata_json["binding_id"] = str(uuid4())
    assert await store.lookup(modern, resource, "public.reports") is None
    session.execute.side_effect = OSError("cache unavailable")
    assert await store.lookup(modern, resource, "public.reports") is None
    session.execute.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await store.lookup(modern, resource, "public.reports")


@pytest.mark.asyncio
async def test_recovery_is_explicit_and_bounded(modern):
    """原失敗は残し、他 Run や二度目の訂正を拒否する。"""
    root = SimpleNamespace(
        id=uuid4(),
        run_id=modern.run_id,
        integration_id=modern.tool.integration_id,
        status="FAILED",
        capability_version="database.read/v2",
        arguments_summary={
            "database": {"table": "public.reports", "binding_id": str(modern.tool.binding_id)}
        },
        error_json={"database": {"reason_code": "undefined_column", "stage": "row_read"}},
    )
    session = AsyncMock()
    session.get.return_value, session.scalar.return_value = root, None
    store = DatabaseObservations(
        Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session)))
    )
    await store.recovery(modern, "public.reports", str(root.id))
    session.scalar.return_value = uuid4()
    with pytest.raises(ValueError, match="One schema"):
        await store.recovery(modern, "public.reports", str(root.id))
    root.run_id = uuid4()
    with pytest.raises(ValueError, match="original"):
        await store.recovery(modern, "public.reports", str(root.id))
    assert root.status == "FAILED"


@pytest.mark.asyncio
async def test_schema_cache_hit_is_not_authorization(provider, modern, schema, resource):
    """cache hit の復権迂回を防ぎ、元 Evidence を返す前に撤権を拒否する。"""
    impl, source, binding, _ = provider
    ctx = replace(modern, tool=replace(modern.tool, capability="database.describe/v1"))
    impl._observations.lookup = AsyncMock(
        return_value={
            "table_schema": schema,
            "schema_checksum": "sha256:" + "a" * 64,
            "observed_at": "2026-09-16T01:00:00Z",
            "observation_refs": ["ev_original"],
        }
    )
    binding.side_effect = [resource, RunBindingError("scope_denied", "Revoked", retryable=False)]
    with pytest.raises(ToolProviderError, match="Revoked"):
        await impl.execute(ctx, {"table": "public.reports", "purpose": "Inspect"})
    source.describe.assert_not_called()


@pytest.mark.asyncio
async def test_corrected_read_requires_successful_matching_schema_evidence(modern):
    """訂正は同一表・原失敗へ結ばれた最新確認を要求し、成功回数から推定しない。"""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    identity = {"table": "public.reports", "binding_id": str(modern.tool.binding_id)}
    root = SimpleNamespace(
        id=uuid4(),
        run_id=modern.run_id,
        created_at=now,
        integration_id=modern.tool.integration_id,
        status="FAILED",
        capability_version="database.read/v2",
        arguments_summary={"database": identity},
        error_json={"database": {"reason_code": "undefined_column", "stage": "row_read"}},
    )
    session = AsyncMock()
    session.get.return_value, session.scalar.return_value = root, None
    schema_call = SimpleNamespace(
        integration_id=modern.tool.integration_id,
        arguments_summary={"database": {**identity, "recovery_from": str(root.id)}},
    )
    evidence = SimpleNamespace(created_at=now)
    session.execute.return_value = Mock(one_or_none=Mock(return_value=(evidence, schema_call)))
    store = DatabaseObservations(
        Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session)))
    )
    await store.recovery(modern, "public.reports", str(root.id), "ev_schema")
    schema_call.arguments_summary["database"]["table"] = "private.reports"
    with pytest.raises(ValueError, match="does not belong"):
        await store.recovery(modern, "public.reports", str(root.id), "ev_schema")
    assert root.status == "FAILED"


@pytest.mark.asyncio
async def test_next_segment_reuses_exact_frozen_schema_without_source_read(
    provider, modern, schema, resource
):
    """同 Segment の再構築は保存済み事実を保ち、再観測で checksum を変えない。"""
    impl, source, _, _ = provider
    fact = {
        "table": "public.reports",
        "binding_id": str(modern.tool.binding_id),
        "integration_id": str(modern.tool.integration_id),
        "binding_checksum": resource.checksum,
        "table_schema": schema,
        "schema_checksum": "sha256:" + sha256_hex(canonical_json(schema)),
        "observed_at": "2026-09-16T01:00:00Z",
        "observation_refs": ["ev_original"],
    }
    impl._observations.segment_index = AsyncMock(return_value=([fact], None))
    impl._observations.tables = AsyncMock()
    claimed = SimpleNamespace(
        task_snapshot_json={"runtime_policy": "skillmind.runtime/v2"},
        run_id=modern.run_id,
        run_segment_id=uuid4(),
        run_attempt_id=modern.run_attempt_id,
        project_id=modern.project_id,
        actor_id=modern.user_id,
    )
    tools = [replace(modern.tool, capability="database.describe/v1")]
    assert await impl.project_facts(claimed, tools, modern.workspace) == [fact]
    assert await impl.project_facts(claimed, tools, modern.workspace) == [fact]
    impl._observations.tables.assert_not_called()
    source.describe.assert_not_called()
    source.read.assert_not_called()
