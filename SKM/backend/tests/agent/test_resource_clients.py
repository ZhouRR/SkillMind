"""原生 HTTP/SQL と file 受渡しの境界を、実 file と隔離 transport で検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from skillmind.agent.http_provider import HttpReadProvider
from skillmind.agent.http_source import HttpResourceError, HttpResourceSource
from skillmind.agent.postgres_native import native_statement, query_identity
from skillmind.agent.proposal_files import expand_proposal_file
from skillmind.agent.resource_files import ResourceFileProvider, request_from_file
from skillmind.agent.tool_gateway import ProviderToolResult, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.integrations.http_resource import normalize_config, normalize_scope, validate_request
from tests.agent.test_workspace_provider import _context


def file_context(tmp_path, capability="http.read/v1", provider="http"):
    """既存 fixture に file 読取権と原 ToolCall identity を付ける。"""
    context = _context(tmp_path, capability)
    return replace(
        context,
        tool=replace(
            context.tool,
            provider=provider,
            integration_id=uuid4(),
            binding_id=uuid4(),
            resource_key="api",
        ),
        run=SimpleNamespace(
            permission_snapshot={"allowed_capabilities": [capability, "workspace.read/v1"]}
        ),
        tool_call_id=uuid4(),
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example/a",
        "//other.example/a",
        "/a/../b",
        "/%2e%2e/b",
        "/a\\b",
        "/a?next=b",
        "/a\r\nx",
    ],
)
def test_http_paths_cannot_change_authority_or_escape_prefix(path):
    """正規化前に別宛先・encoded traversal を拒否する。"""
    with pytest.raises(ValueError):
        validate_request(
            {"base_url": "https://api.example.test/base"},
            {"paths": ["/"], "methods": ["GET"]},
            {"path": path},
            write=False,
        )


async def test_http_native_body_and_api_key_reach_only_registered_origin():
    """認証 header は client が挿入し、redirect は追従しない。"""
    received = []

    def respond(request):
        received.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://other.example/", "Set-Cookie": "private"},
            content=b"redirect",
        )

    source = HttpResourceSource(httpx.MockTransport(respond))
    config = normalize_config(
        {
            "base_url": "https://api.example.test/base",
            "auth_mode": "header",
            "credential_header": "X-Redmine-API-Key",
        }
    )
    scope = normalize_scope({"paths": ["/issues"], "methods": ["POST"]}, write_enabled=True)
    result = await source.request(
        config,
        scope,
        {"method": "POST", "path": "/issues", "body": {"issue": {"subject": "sample"}}},
        "fixture-api-value",
        write=True,
    )
    assert len(received) == 1
    assert str(received[0].url) == "https://api.example.test/base/issues"
    assert received[0].headers["X-Redmine-API-Key"] == "fixture-api-value"
    assert json.loads(received[0].content) == {"issue": {"subject": "sample"}}
    assert result.status == 302 and "set-cookie" not in result.headers


async def test_http_no_automatic_retry_or_credential_echo():
    """例外と model 応答へ credential を出さない。"""
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("private transport detail")

    config = {"base_url": "https://api.example.test", "auth_mode": "bearer"}
    scope = {"paths": ["/"], "methods": ["GET"]}
    with pytest.raises(HttpResourceError, match="transport_unconfirmed"):
        await HttpResourceSource(httpx.MockTransport(fail)).request(
            config, scope, {"path": "/items"}, "fixture-value"
        )
    assert len(calls) == 1
    with pytest.raises(HttpResourceError, match="credential_in_response"):
        await HttpResourceSource(
            httpx.MockTransport(lambda _: httpx.Response(200, content=b"fixture-value"))
        ).request(config, scope, {"path": "/items"}, "fixture-value")


async def test_http_response_stays_in_run_file_and_revocation_prevents_publication(tmp_path):
    """完全な正文の file と小さい metadata を分離する。"""
    context = file_context(tmp_path)
    config = {"base_url": "https://api.example.test", "auth_mode": "none"}
    bound = SimpleNamespace(
        integration=SimpleNamespace(config=config), scope={"paths": ["/"], "methods": ["GET"]}
    )
    body = b'{"message":"' + b"x" * 1_100_000 + b'"}'
    provider = HttpReadProvider(
        Mock(),
        source=HttpResourceSource(httpx.MockTransport(lambda _: httpx.Response(200, content=body))),
        secret_resolver=Mock(),
    )
    provider._bound = AsyncMock(return_value=(bound, None))
    result = await provider.execute(context, {"path": "/items"})
    assert (context.workspace.root / result.response["file"]["path"]).read_bytes() == body
    assert len(canonical_json(result.response)) < 1024
    context = replace(context, tool_call_id=uuid4())
    provider._bound = AsyncMock(
        side_effect=[(bound, None), ToolProviderError("scope_denied", "revoked", retryable=False)]
    )
    with pytest.raises(ToolProviderError):
        await provider.execute(context, {"path": "/items"})
    assert not (context.workspace.cwd / "resources" / str(context.tool_call_id)).exists()


@pytest.mark.parametrize(
    "sql",
    [
        "COMMIT",
        "SET ROLE admin",
        "COPY x TO PROGRAM 'x'",
        "CREATE TABLE x(id int)",
        "SELECT 1; DELETE FROM x",
        "SELECT * INTO new_table FROM old_table",
        "SELECT * FROM x FOR UPDATE",
        "WITH gone AS (DELETE FROM x RETURNING *) SELECT * FROM gone",
    ],
)
def test_native_read_rejects_transaction_ddl_and_hidden_mutations(sql):
    """SQL text の keyword 検索でなく PostgreSQL AST で拒否する。"""
    with pytest.raises(ValueError):
        native_statement({"sql": sql}, {"statements": ["SELECT", "DELETE"]}, write=False)


def test_native_sql_preserves_join_cte_parameter_and_statement_permissions():
    """原生構文を表/列の独自 DSL へ変換しない。"""
    sql, parameters, operation = native_statement(
        {
            'sql': (
                'WITH a AS (SELECT * FROM public.items WHERE id=$1) SELECT a.id,b.name FROM a JOIN '
                'public.names b ON b.id=a.id'
            ),
            "parameters": [42],
        },
        {"statements": ["SELECT"]},
        write=False,
    )
    assert "JOIN" in sql and "$1" in sql and parameters == (42,) and operation == "SELECT"
    with pytest.raises(ValueError):
        native_statement(
            {"sql": "DELETE FROM public.items"}, {"statements": ["SELECT", "UPDATE"]}, write=True
        )


async def test_request_files_are_hashed_scoped_and_validated_before_mcp_call(tmp_path):
    """file を使っても原 schema/connection selector の検証を迂回しない。"""
    context = file_context(tmp_path, "mcp.query/v1", "mcp")
    path = context.workspace.cwd / "request.json"
    raw = b'{"name":"inspect","arguments":{"appId":"sample"}}'
    path.write_bytes(raw)
    args = {
        "request_file": "workspace/request.json",
        "expected_hash": "sha256:" + sha256_hex(raw),
        "response_mode": "file",
    }
    inner = Mock()
    inner.execute = AsyncMock(
        return_value=ProviderToolResult(
            response={"status": "success", "provider": "mcp", "result": {"text": "original"}},
            evidence=(),
        )
    )
    wrapped = ResourceFileProvider(
        inner,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "arguments"],
            "properties": {"name": {"const": "inspect"}, "arguments": {"type": "object"}},
        },
    )
    result = await wrapped.execute(context, args)
    assert inner.execute.await_args.args[1] == json.loads(raw)
    assert (
        json.loads((context.workspace.root / result.response["file"]["path"]).read_bytes())[
            "result"
        ]["text"]
        == "original"
    )
    path.write_bytes(b"{}")
    with pytest.raises(ToolProviderError):
        await request_from_file(context, args)
    assert inner.execute.await_count == 1


async def test_proposal_file_resolves_exact_bytes_without_model_transcription(tmp_path):
    """差替え・symlink・root 逃逸で承認対象を変更できない。"""
    from tests.agent.test_tool_policy import _database_proposal

    context = file_context(tmp_path)
    proposal, _, tool, _ = _database_proposal()
    proposal["changes"][0]["value"]["values"]["message"] = "original"
    raw = json.dumps(proposal).encode()
    path = context.workspace.cwd / "proposal.json"
    path.write_bytes(raw)
    run = SimpleNamespace(
        workspace=context.workspace,
        tools=(tool,),
        permission_snapshot={"allowed_capabilities": [tool.capability, "workspace.read/v1"]},
    )
    args = {
        "request_file": "workspace/proposal.json",
        "expected_hash": "sha256:" + sha256_hex(raw),
        "evidence_refs": ["ev_one"],
    }
    expanded = await expand_proposal_file(run, args)
    assert expanded["changes"][0]["value"]["values"]["message"] == "original"
    assert expanded["evidence_refs"] == ["ev_one"]
    path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        await expand_proposal_file(run, args)
    with pytest.raises(ValueError):
        await expand_proposal_file(run, {**args, "request_file": "workspace/../proposal.json"})


async def test_git_workspace_prepares_exact_edited_bytes_and_preserves_drafts(tmp_path):
    """日本語 file の編集をそのまま提案にし、再 checkout で草稿を失わない。"""
    from contextlib import asynccontextmanager

    from skillmind.agent.repository_workspace import RepositoryWorkspaceProvider
    from skillmind.effects.proposal import parse_change_proposal_request

    context = file_context(tmp_path, "repository.workspace/v1", "git")
    original = "# 元の計画\n".encode()
    edited = "# 修正済み計画\n完全な内容です。\n".encode()
    session = SimpleNamespace(
        revision="a" * 40,
        scope_paths=("plans",),
        list_files=AsyncMock(
            return_value=SimpleNamespace(
                entries=[SimpleNamespace(path="plans/計画.md", size=len(original))], skipped=[]
            )
        ),
        read_file=AsyncMock(return_value=original),
    )

    @asynccontextmanager
    async def opened(**kwargs):
        yield session

    provider = RepositoryWorkspaceProvider(SimpleNamespace(open=opened))
    result = await provider.execute(context, {"action": "checkout", "revision": "main"})
    index = json.loads((context.workspace.root / result.response["file"]["path"]).read_bytes())
    local = index["files"][0]["local_path"]
    (context.workspace.root / local).write_bytes(edited)
    with pytest.raises(ToolProviderError):
        await provider.execute(
            replace(context, tool_call_id=uuid4()), {"action": "checkout", "revision": "main"}
        )
    assert (context.workspace.root / local).read_bytes() == edited
    args = {
        "action": "prepare_commit",
        "revision": "main",
        "branch": "main",
        "summary": "更新",
        "files": [
            {
                "path": "plans/計画.md",
                "action": "SET",
                "source_path": local,
                "expected_hash": "sha256:" + sha256_hex(edited),
            }
        ],
    }
    result = await provider.execute(replace(context, tool_call_id=uuid4()), args)
    proposal = json.loads((context.workspace.root / result.response["file"]["path"]).read_bytes())
    assert proposal["changes"] == [
        {"path": "/files/plans/計画.md", "action": "SET", "value": edited.decode()}
    ]
    proposal["evidence_refs"] = ["ev_original"]
    parse_change_proposal_request(proposal, request_identity="fixture-call")
    args["files"][0]["path"] = "private/secret.md"
    with pytest.raises(ToolProviderError):
        await provider.execute(replace(context, tool_call_id=uuid4()), args)


@pytest.mark.parametrize(
    "state,code",
    [
        ("42703", "invalid_request"),
        ("42P01", "not_found"),
        ("42501", "scope_denied"),
        ("08006", "unavailable"),
    ],
)
async def test_native_sql_diagnostics_do_not_regress_to_generic_connection_errors(
    tmp_path, state, code
):
    """元 SQLSTATE に応じて修正/権限/接続を区別し、driver 本文は返さない。"""
    from sqlalchemy.exc import DBAPIError

    from skillmind.agent.postgres_native_provider import NativeDatabaseProvider

    underlying = Exception("private SQL and parameter values")
    underlying.sqlstate = state
    source = SimpleNamespace(read=AsyncMock(side_effect=DBAPIError("private SQL", {}, underlying)))
    provider = NativeDatabaseProvider(Mock(), source=source, secret_resolver=Mock())
    bound = SimpleNamespace(
        integration=SimpleNamespace(config={}), scope={"statements": ["SELECT"]}
    )
    provider._bound = AsyncMock(return_value=(bound, "fixture-password"))
    with pytest.raises(ToolProviderError) as failure:
        await provider.execute(
            file_context(tmp_path, "database.query/v1", "postgres"),
            {"sql": "SELECT unknown FROM items"},
        )
    assert failure.value.code == code
    assert "private" not in failure.value.message
    assert provider._bound.await_count == 2


def test_native_upsert_cannot_bypass_update_permission():
    """INSERT の許可だけで ON CONFLICT UPDATE を実行しない。"""
    statement = {"sql": "INSERT INTO public.items(id) VALUES(1) ON CONFLICT(id) DO UPDATE SET id=2"}
    with pytest.raises(ValueError, match="UPDATE permission"):
        native_statement(statement, {"statements": ["SELECT", "INSERT"]}, write=True)
    assert (
        native_statement(statement, {"statements": ["SELECT", "INSERT", "UPDATE"]}, write=True)[2]
        == "INSERT"
    )


@pytest.mark.parametrize("truncated", [False, True])
async def test_native_query_evidence_preserves_exact_request_and_binding(tmp_path, truncated):
    """配送方法や purpose を除いた query identity と元 binding を監査へ保存する。"""
    from skillmind.agent.postgres_native_provider import NativeDatabaseProvider

    context = file_context(tmp_path, "database.query/v1", "postgres")
    request = {"sql": "SELECT status FROM public.items WHERE id=$1", "parameters": [1], "limit": 2}
    result = {"rows": [], "truncated": truncated}
    bound = SimpleNamespace(
        integration=SimpleNamespace(config={}),
        scope={"statements": ["SELECT"]},
        checksum="sha256:" + "a" * 64,
    )

    async def read(*args, authorize):
        """外部 I/O だけ省略し、Source の公開前権限確認を維持する。"""
        await authorize()
        return result

    source = SimpleNamespace(read=AsyncMock(side_effect=read))
    provider = NativeDatabaseProvider(Mock(), source=source, secret_resolver=Mock())
    provider._bound = AsyncMock(return_value=(bound, "fixture-password"))
    raw = json.dumps({**request, "purpose": "Check absence"}).encode()
    (context.workspace.cwd / "query.json").write_bytes(raw)
    response = await provider.execute(
        context,
        {
            "request_file": "workspace/query.json",
            "expected_hash": "sha256:" + sha256_hex(raw),
            "response_mode": "file",
        },
    )
    evidence = response.evidence[0]
    assert evidence.source_locator["query_identity"] == query_identity(request, bound.scope)
    assert evidence.metadata["binding_checksum"] == bound.checksum
    assert evidence.metadata["truncated"] is truncated
    assert evidence.content_hash == response.response["file"]["content_hash"]
    assert evidence.content_hash == "sha256:" + sha256_hex(canonical_json(result))
    assert provider._bound.await_count == 2
