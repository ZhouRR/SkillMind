"""固定 Codex CLI をローカル合成 endpoint へ接続し、wire と MCP 境界を検証する。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from openai_codex.client import CodexConfig

from skillmind.agent import codex_completion, codex_engine
from skillmind.agent.codex_completion import CodexCompletionClient
from skillmind.agent.codex_engine import CodexAgentSdkEngine
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration, create_codex_client
from skillmind.agent.context_builder import ContractStore, _change_propose_tool_definition
from skillmind.agent.domain import AgentEventType, AgentSessionRef, ResumeContext
from skillmind.agent.session_store import TranscriptKey
from skillmind.agent.tool_gateway import ToolRegistry
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.proposal_continuation import ResolvedProposal
from tests.agent.test_codex_schema import assert_strict_schema
from tests.agent.test_session_store import MemoryTranscriptBackend
from tests.agent.test_tool_gateway import CsvIssueProvider, MemoryAuditWriter, _context, _registry


def _wire_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """通常 Responses と additional_tools の両形状から実際の Tool 定義を抽出する。"""

    return list(body.get("tools") or []) + [
        tool
        for item in body.get("input", [])
        if item.get("type") == "additional_tools"
        for tool in item.get("tools", [])
    ]


def _wire_tool_names(body: dict[str, Any]) -> set[str]:
    """Namespace を展開し、model が呼べる名前を境界検査用に列挙する。"""

    names: set[str] = set()
    for tool in _wire_tools(body):
        if tool.get("type") == "namespace":
            names.update(f"{tool['name']}.{nested['name']}" for nested in tool.get("tools", []))
        else:
            names.add(tool.get("name", tool["type"]))
    return names


class SyntheticEndpoint(ThreadingHTTPServer):
    """モデル応答の開始と保留を同期し、取消時の native process を検査する。"""

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self.reply_allowed = threading.Event()
        self.reply_allowed.set()
        self.request_seen = threading.Event()
        self.failure: dict[str, Any] | None = None

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        """取消による接続終了だけを許容し、その他の fixture 障害は表示する。"""

        if not isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


@pytest.fixture
def endpoint() -> Iterator[tuple[SyntheticEndpoint, list[dict[str, Any]]]]:
    """実 model/認証へ通信しない合成 Responses SSE server を起動する。"""

    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """原要求を保存し、一つの MCP 呼出しと最終 JSON を決定的に返す。"""

        def log_message(self, *_: object) -> None:
            """合成 server の access log を省略する。"""

        def do_POST(self) -> None:
            """初回だけ advertised MCP Tool を呼び、以降は結果候補を返す。"""

            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            schema = body["text"]["format"]["schema"]
            assert_strict_schema(schema)
            server.request_seen.set()
            if not server.reply_allowed.wait(timeout=10):
                return
            if server.failure is not None:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(server.failure).encode())
                return
            result = (
                json.dumps({"result_json": '{"ok":true}'})
                if "result_json" in schema["properties"] else '{"ok":true}'
            )
            if "present" in schema["properties"].get("ok", {}).get("properties", {}):
                result = '{"ok":{"present":true,"value":true}}'
            item: dict[str, Any] = {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": result,
                        "annotations": [],
                    }
                ],
            }
            if len(requests) in {1, 3} and "mcp__skillmind.issue_read_v1" in _wire_tool_names(body):
                item = {
                    "id": f"fc_fixture_{len(requests)}",
                    "type": "function_call",
                    "call_id": f"call_fixture_{len(requests)}",
                    "name": "issue_read_v1",
                    "namespace": "mcp__skillmind",
                    "arguments": json.dumps({"issue_ref": "TICKET-1", "purpose": "analysis"}),
                    "status": "completed",
                }
            elif len(requests) == 1 and "mcp__skillmind.change_propose_v1" in _wire_tool_names(
                body
            ):
                contracts = Path(__file__).resolve().parents[3] / "contracts"
                item = {
                    "type": "function_call",
                    "id": "fc_proposal",
                    "call_id": "call_proposal",
                    "namespace": "mcp__skillmind",
                    "name": "change_propose_v1",
                    "arguments": (
                        contracts / "examples/change-propose-request.v1.json"
                    ).read_text(),
                    "status": "completed",
                }
            response = {
                "id": f"resp_{len(requests)}",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.6-terra",
                "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }
            events = [
                (
                    "response.created",
                    {"response": {**response, "status": "in_progress", "output": []}},
                ),
                ("response.output_item.added", {"output_index": 0, "item": item}),
                ("response.output_item.done", {"output_index": 0, "item": item}),
                ("response.completed", {"response": response}),
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for method, payload in events:
                self.wfile.write(
                    (
                        f"event: {method}\ndata: "
                        + json.dumps(
                            {
                                "type": method,
                                **payload,
                            }
                        )
                        + "\n\n"
                    ).encode()
                )
            self.wfile.flush()

    server = SyntheticEndpoint(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.reply_allowed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _local_client(monkeypatch: pytest.MonkeyPatch, server: ThreadingHTTPServer) -> None:
    """本番 boundary を保ち、model Provider だけを合成 loopback endpoint に置き換える。"""

    def factory(config: CodexConfig) -> Any:
        """本番の Tool/effort 設定は変更せず、架空認証をローカル fixture だけへ渡す。"""

        overrides = tuple(
            value
            for value in config.config_overrides
            if not value.startswith(("model_provider=", "forced_login_method="))
        )
        overrides += (
            'model_provider="fixture"',
            'model_providers.fixture.name="fixture"',
            f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
            'model_providers.fixture.wire_api="responses"',
            'model_providers.fixture.env_key="SKM_SYNTHETIC_KEY"',
            "model_providers.fixture.request_max_retries=0",
            "model_providers.fixture.stream_max_retries=0",
            "model_providers.fixture.supports_websockets=false",
        )
        client = create_codex_client(
            replace(
                config,
                config_overrides=overrides,
                env={**(config.env or {}), "SKM_SYNTHETIC_KEY": "synthetic-key"},
            )
        )
        return client

    monkeypatch.setattr(codex_engine, "create_codex_client", factory)
    monkeypatch.setattr(codex_completion, "create_codex_client", factory)


@pytest.mark.parametrize(("field_schema", "required"), [
    ({"type": "boolean"}, True), ({"type": ["boolean", "null"]}, True),
    ({"const": True}, True), ({"type": ["boolean", "null"]}, False),
])
async def test_native_completion_preserves_model_effort_and_has_no_resource_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[ThreadingHTTPServer, list[dict[str, Any]]],
    field_schema: dict[str, Any],
    required: bool,
) -> None:
    """実 SDK/CLI が native Schema と max を送り、host/builtin Tool を付加しない。"""

    server, requests = endpoint
    _local_client(monkeypatch, server)
    configuration = CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex")
    async with asyncio.timeout(30):
        result = await CodexCompletionClient(configuration).complete(
            system_prompt="Return JSON.",
            user_message="Return ok true.",
            response_schema={
                "type": "object",
                "properties": {"ok": field_schema},
                "required": ["ok"] if required else [],
                "additionalProperties": False,
            },
            model="gpt-5.6-terra",
            parameters={},
        )
    assert result.structured_output == {"ok": True}
    assert len(requests) == 1
    assert requests[0]["model"] == "gpt-5.6-terra"
    assert requests[0]["reasoning"]["effort"] == "max"
    assert requests[0]["text"]["format"]["strict"] is True
    assert not _wire_tool_names(requests[0]) - {"update_plan", "request_user_input"}


async def test_native_gateway_and_resume_keep_original_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[ThreadingHTTPServer, list[dict[str, Any]]],
) -> None:
    """実 MCP 呼出しが Gateway の Evidence を先に保存し、別 Attempt は原 thread を再開する。"""

    server, requests = endpoint
    _local_client(monkeypatch, server)
    provider = CsvIssueProvider()
    registry = _registry(provider)
    context = replace(_context(tmp_path, registry), model="gpt-5.6-terra")
    writer = MemoryAuditWriter()
    backend = MemoryTranscriptBackend()
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex"),
        runtime_factory=lambda run: registry.build_gateway_runtime(run, audit_writer=writer),
        transcript_backend=backend,
    )
    async with asyncio.timeout(30):
        events = [event async for event in engine.execute(context)]
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in events]
    assert events[-1].payload["structured_output"] == {"ok": True}
    assert len(writer.completed) == 1, {
        "tools": requests[0].get("tools"),
        "outputs": [
            item
            for body in requests
            for item in body.get("input", [])
            if item.get("type") == "function_call_output"
        ],
        "events": [(event.event_type.value, event.payload) for event in events],
    }
    assert len(requests) == 2
    assert _wire_tool_names(requests[0]) <= {
        "mcp__skillmind.issue_read_v1",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "request_user_input",
    }
    assert events[-1].payload["structured_output"] == {"ok": True}
    assert all(body["reasoning"]["effort"] == "max" for body in requests)
    session = AgentSessionRef(context.run_id, context.run_attempt_id, events[0].agent_session_id)
    async with asyncio.timeout(30):
        resumed = [
            event
            async for event in engine.resume(
                ResumeContext(
                    replace(context, run_attempt_id=uuid4()),
                    session,
                    input_text="Continue with final JSON.",
                )
            )
        ]
    assert resumed[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in resumed]
    assert resumed[0].agent_session_id == session.session_id
    assert len(writer.completed) == 2
    assert len(requests) == 4


async def test_native_proposal_pauses_before_provider_and_resumes_original_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[ThreadingHTTPServer, list[dict[str, Any]]],
) -> None:
    """制御要求は native turn 停止後にだけ渡し、別 Attempt は原要求の回执を検証する。"""

    server, _requests = endpoint
    _local_client(monkeypatch, server)
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    registry = ToolRegistry((_change_propose_tool_definition(ContractStore(contracts)),))
    base = _context(tmp_path, _registry(CsvIssueProvider()))
    context = replace(
        base,
        model="gpt-5.6-terra",
        tools=(registry.resolve_unbound("change.propose/v1", execution_profile="GUIDED"),),
        permission_snapshot={"allowed_capabilities": ["change.propose/v1"]},
    )
    writer = MemoryAuditWriter()
    backend = MemoryTranscriptBackend()
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex"),
        runtime_factory=lambda run: registry.build_gateway_runtime(run, audit_writer=writer),
        transcript_backend=backend,
    )
    async with asyncio.timeout(30):
        events = [event async for event in engine.execute(context)]
    assert events[-1].event_type is AgentEventType.CHANGE_PROPOSED, [e.payload for e in events]
    assert not writer.completed and not writer.invocations
    session_id = events[0].agent_session_id
    entries = backend.entries[TranscriptKey(f"codex:{context.run_id}", session_id)]
    assert entries[-1]["type"] == "codex_terminal"
    original = json.loads((contracts / "examples/change-propose-request.v1.json").read_text())
    assert events[-1].payload["change_proposal_request"] == original
    resolved = ResolvedProposal(
        sha256_hex(canonical_json(original)), session_id, "cp_fixture", "REJECTED"
    )
    async with asyncio.timeout(30):
        resumed = [
            event
            async for event in engine.resume(
                ResumeContext(
                    replace(context, run_attempt_id=uuid4(), resolved_proposal=resolved),
                    AgentSessionRef(context.run_id, context.run_attempt_id, session_id),
                    input_text="The proposal was rejected. Return final JSON.",
                )
            )
        ]
    assert resumed[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in resumed]
    assert resumed[0].agent_session_id == session_id
    assert not writer.completed


async def test_native_output_limit_rejects_completed_message_without_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[ThreadingHTTPServer, list[dict[str, Any]]],
) -> None:
    """delta のない最終本文も byte 上限で拒否し、成功や公開全文に投影しない。"""

    server, _requests = endpoint
    _local_client(monkeypatch, server)
    registry = _registry(CsvIssueProvider())
    base = _context(tmp_path, registry)
    context = replace(base, model="gpt-5.6-terra", limits=replace(base.limits, max_output_bytes=1))
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex"),
        runtime_factory=lambda run: registry.build_gateway_runtime(
            run, audit_writer=MemoryAuditWriter()
        ),
        transcript_backend=MemoryTranscriptBackend(),
    )
    async with asyncio.timeout(30):
        events = [event async for event in engine.execute(context)]
    assert events[-1].event_type is AgentEventType.ENGINE_FAILED
    assert events[-1].payload["reason"] == "run_limit_exceeded"
    assert not any(
        event.event_type in {AgentEventType.RESULT_COMPLETED, AgentEventType.TEXT_COMPLETED}
        for event in events
    )


async def test_native_interrupt_waits_for_owned_runtime_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[SyntheticEndpoint, list[dict[str, Any]]],
) -> None:
    """応答待ちの native turn を停止し、Gateway を閉じてから取消完了を返す。"""

    server, requests = endpoint
    server.reply_allowed.clear()
    _local_client(monkeypatch, server)
    registry = _registry(CsvIssueProvider())
    context = replace(_context(tmp_path, registry), model="gpt-5.6-terra")
    writer = MemoryAuditWriter()
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex"),
        runtime_factory=lambda run: registry.build_gateway_runtime(run, audit_writer=writer),
        transcript_backend=MemoryTranscriptBackend(),
    )
    events = []

    async def consume() -> None:
        """生成器を進め続け、interrupt と終了処理の順序を検証する。"""

        async for event in engine.execute(context):
            events.append(event)

    async with asyncio.timeout(30):
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(server.request_seen.wait, 10)
            session = AgentSessionRef(
                context.run_id, context.run_attempt_id, events[0].agent_session_id
            )
            await engine.interrupt(session)
            await task
            assert events[-1].event_type is AgentEventType.SESSION_INTERRUPTED
            assert not engine._active
            assert not writer.invocations
            assert len(requests) == 1
        finally:
            server.reply_allowed.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("include_status", [False, True])
async def test_native_schema_rejection_retains_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[SyntheticEndpoint, list[dict[str, Any]]],
    include_status: bool,
) -> None:
    """実 CLI の HTTP 400 を分類し、Provider 本文を上位へ漏らさない。"""

    from skillmind.skills.model_interpreter import ModelProviderError

    server, requests = endpoint
    server.failure = {"error": {
        "type": "invalid_request_error", "code": "invalid_json_schema",
        "message": "private fixture content", "param": "text.format.schema",
    }}
    if include_status:
        server.failure["status"] = 400
    _local_client(monkeypatch, server)
    configuration = CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex")
    with pytest.raises(ModelProviderError) as caught:
        async with asyncio.timeout(30):
            await CodexCompletionClient(configuration).complete(
                system_prompt="Return JSON.", user_message="Return ok true.",
                response_schema={"type": "object"}, model="gpt-5.6-terra", parameters={},
            )
    assert len(requests) == 1
    assert caught.value.detail == (
        "codex:invalid_json_schema" + ("; http_status=400" if include_status else "")
    )
    assert "private" not in str(caught.value)
