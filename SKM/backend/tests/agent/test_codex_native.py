"""固定 Codex CLI をローカル合成 endpoint へ接続し、wire と MCP 境界を検証する。"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from openai_codex.client import CodexConfig
from skillmind.agent import codex_completion, codex_engine
from skillmind.agent.codex_completion import CodexCompletionClient
from skillmind.agent.codex_engine import CodexAgentSdkEngine
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration, create_codex_client
from skillmind.agent.contract_store import (
    ContractStore,
)
from skillmind.agent.domain import AgentEventType, AgentSessionRef, ResumeContext
from skillmind.agent.session_store import TranscriptKey
from skillmind.agent.tool_catalog import (
    _change_propose_tool_definition,
)
from skillmind.agent.tool_gateway import ToolRegistry
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.proposal_continuation import ResolvedProposal
from skillmind.skills.candidate import CANDIDATE_SCHEMA_ID, candidate_schema
from skillmind.skills.direct_candidate import CANDIDATE_SCHEMA_ID as DIRECT_SCHEMA_ID
from skillmind.skills.direct_candidate import candidate_schema as direct_schema
from tests.agent.test_codex_schema import assert_strict_schema
from tests.agent.test_continuation_prompt import compiled, context_with_brief
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
        self.failure_after_requests = 0
        self.tool_request_numbers = {1, 3}
        self.stream_text = False
        self.whitespace_loop = False
        self.native_candidate: dict[str, Any] | None = None

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
            if schema.get("$id") in {CANDIDATE_SCHEMA_ID, DIRECT_SCHEMA_ID}:
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(server.native_candidate)
            else:
                assert_strict_schema(schema)
            server.request_seen.set()
            if not server.reply_allowed.wait(timeout=10):
                return
            if server.failure is not None and len(requests) > server.failure_after_requests:
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
            if server.native_candidate is not None:
                result = json.dumps(server.native_candidate, ensure_ascii=False)
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
            if (len(requests) in server.tool_request_numbers
                and "mcp__skillmind.issue_read_v1" in _wire_tool_names(body)):
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
            if server.stream_text and item["type"] == "message":
                text = item["content"][0]["text"]
                empty_part = {"type": "output_text", "text": "", "annotations": []}
                position = {"item_id": item["id"], "output_index": 0, "content_index": 0}
                events[1:2] = [
                    ("response.output_item.added", {
                        "output_index": 0, "item": {**item, "content": [], "status": "in_progress"},
                    }),
                    ("response.content_part.added", {**position, "part": empty_part}),
                    *[("response.output_text.delta", {**position, "delta": char}) for char in text],
                    ("response.output_text.done", {**position, "text": text}),
                    ("response.content_part.done", {**position, "part": item["content"][0]}),
                ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if server.whitespace_loop:
                # 完了 event のない空白 loop を実 CLI へ流し、client の中断で接続が閉じる。
                position = {"item_id": item["id"], "output_index": 0, "content_index": 0}
                opening = [events[0], ("response.output_item.added", {
                    "output_index": 0, "item": {**item, "content": [], "status": "in_progress"},
                })]
                for method, payload in opening:
                    self.wfile.write(("data: " + json.dumps({"type": method, **payload})
                                      + "\n\n").encode())
                for _ in range(10000):
                    self.wfile.write(("data: " + json.dumps({
                        "type": "response.output_text.delta", **position, "delta": " " * 128,
                    }) + "\n\n").encode())
                    self.wfile.flush()
                    time.sleep(.002)
                return
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
    context = replace(context_with_brief(tmp_path), model="gpt-5.6-terra")
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
    next_brief = deepcopy(context.task_brief)
    next_brief["identity"]["segment_no"] = 2
    next_brief["checkpoint"]["summary"] = "Continue with final JSON."
    next_context = compiled(replace(context, run_attempt_id=uuid4()), next_brief)
    async with asyncio.timeout(30):
        resumed = [
            event
            async for event in engine.resume(
                ResumeContext(
                    next_context,
                    session,
                )
            )
        ]
    assert resumed[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in resumed]
    assert resumed[0].agent_session_id == session.session_id
    assert len(writer.completed) == 2
    assert len(requests) == 4
    resumed_wire = json.dumps(requests[-1], ensure_ascii=False)
    assert resumed_wire.count("Frozen Skill source documents") == 1
    assert "Audited continuation changes" in resumed_wire


async def test_native_streamed_text_is_coalesced_without_losing_content_or_terminal_order(
    tmp_path, monkeypatch, endpoint,
):
    """一文字ごとの実 SDK delta をまとめ、全文・最終候補・単調 event 順序を維持する。"""

    server, _ = endpoint
    server.stream_text = True
    _local_client(monkeypatch, server)
    registry = _registry(CsvIssueProvider())
    context = replace(_context(tmp_path, registry), model="gpt-5.6-terra", tools=())
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration(context.model, "max", tmp_path / "codex"),
        runtime_factory=lambda run: registry.build_gateway_runtime(
            run, audit_writer=MemoryAuditWriter(),
        ),
        transcript_backend=MemoryTranscriptBackend(),
    )
    async with asyncio.timeout(30):
        events = [event async for event in engine.execute(context)]
    chunks = [
        event.payload["text"] for event in events if event.event_type is AgentEventType.TEXT_DELTA
    ]
    text = next(event.payload["text"] for event in events
                if event.event_type is AgentEventType.TEXT_COMPLETED)
    assert "".join(chunks) == text
    assert 0 < len(chunks) < len(text)
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED
    assert events[-1].payload["structured_output"] == {"ok": True}
    assert [event.sequence for event in events] == sorted({event.sequence for event in events})


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


async def test_native_capacity_failure_resumes_same_thread_and_keeps_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[SyntheticEndpoint, list[dict[str, Any]]],
) -> None:
    """合成容量エラー後、実 CLI の原会話を継承し、済み Tool を再実行しない。"""
    from skillmind.runs.capacity_retry import MODEL_CAPACITY_CODE

    server, requests = endpoint
    server.failure_after_requests = 1
    server.tool_request_numbers = {1}
    server.failure = {"error": {"code": "server_overloaded", "message": "private fixture text"}}
    _local_client(monkeypatch, server)
    registry = _registry(CsvIssueProvider())
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
    assert events[-1].event_type is AgentEventType.ENGINE_FAILED
    assert events[-1].payload["code"] == MODEL_CAPACITY_CODE
    assert events[-1].payload["retryable"] is True
    assert "private" not in str(events[-1].payload)
    session_id = events[0].agent_session_id
    key = TranscriptKey(f"codex:{context.run_id}", session_id)
    assert backend.entries[key][-1]["failure_detail"] == "codex:server_overloaded"
    completed_before = len(writer.completed)
    assert completed_before > 0
    server.failure = None
    async with asyncio.timeout(30):
        resumed = [
            event
            async for event in engine.resume(
                ResumeContext(
                    replace(context, run_attempt_id=uuid4()),
                    AgentSessionRef(context.run_id, context.run_attempt_id, session_id),
                )
            )
        ]
    assert resumed[-1].event_type is AgentEventType.RESULT_COMPLETED
    assert resumed[0].agent_session_id == session_id
    assert len(writer.completed) == completed_before
    assert len(requests) == 3
    assert "call_fixture_1" in json.dumps(requests[-1]["input"])


@pytest.mark.parametrize("with_progress", [False, True])
async def test_native_whitespace_loop_interrupts_and_reaps_its_only_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[SyntheticEndpoint, list[dict[str, Any]]], with_progress: bool,
) -> None:
    """実 SDK/CLI の空白 stream を止め、進行購読なしでも再送と残存 process を防ぐ。"""

    from skillmind.skills.interpreter_execution import MODEL_OUTPUT_WHITESPACE_LIMIT
    from skillmind.skills.model_interpreter import ModelProviderError

    server, requests = endpoint
    server.whitespace_loop = True
    _local_client(monkeypatch, server)
    factory = codex_completion.create_codex_client
    processes = []
    interrupts = []

    def tracked_client(config):
        """実 process handle と実 interrupt を観測するだけで protocol を置換しない。"""

        client = factory(config)
        start = client.start
        interrupt = client.turn_interrupt

        def tracked_start():
            """起動直後の handle を、SDK が close で消す前に保持する。"""

            start()
            processes.append(client._proc)

        def tracked_interrupt(thread_id, turn_id):
            """実 RPC の thread/turn が固定されていることを確認する。"""

            interrupts.append((thread_id, turn_id))
            return interrupt(thread_id, turn_id)

        client.start = tracked_start
        client.turn_interrupt = tracked_interrupt
        return client

    monkeypatch.setattr(codex_completion, "create_codex_client", tracked_client)
    chunks = []

    async def progress(delta: str) -> None:
        """表示へ届いた文字数だけを集計し、閾値後は送出されないことを検査する。"""

        chunks.append(delta)

    configuration = CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex")
    async with asyncio.timeout(30):
        with pytest.raises(ModelProviderError) as caught:
            await CodexCompletionClient(configuration).complete(
                system_prompt="Return JSON.", user_message="Return ok true.",
                response_schema={"type": "object"}, model="gpt-5.6-terra", parameters={},
                on_text_delta=progress if with_progress else None,
            )
    assert caught.value.detail == MODEL_OUTPUT_WHITESPACE_LIMIT
    assert len(requests) == len(processes) == len(interrupts) == 1
    assert processes[0].poll() is not None
    assert sum(map(len, chunks)) < 4096


@pytest.mark.parametrize("direct", [False, True])
async def test_native_skill_candidate_uses_unencoded_schema_and_original_model(
    direct: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: tuple[SyntheticEndpoint, list[dict[str, Any]]],
) -> None:
    """固定 SDK/CLI で単一候補を往復し、Schema と Astra/medium をそのまま送る。"""

    server, requests = endpoint
    _local_client(monkeypatch, server)
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    schema = direct_schema(contracts) if direct else candidate_schema(contracts)
    server.native_candidate = json.loads(
        (contracts / (
            "examples/skill-candidate.v2.json" if direct else "examples/skill-candidate.v1.json"
        )).read_text()
    )
    configuration = CodexRuntimeConfiguration("gpt-6-astra", "medium", tmp_path / "codex")
    async with asyncio.timeout(30):
        result = await CodexCompletionClient(configuration).complete(
            system_prompt="Return the candidate as native JSON.", user_message="Synthetic fixture.",
            response_schema=schema, model="gpt-6-astra", parameters={},
        )
    assert result.structured_output == server.native_candidate
    assert len(requests) == 1
    assert requests[0]["model"] == "gpt-6-astra"
    assert requests[0]["reasoning"]["effort"] == "medium"
    assert requests[0]["text"]["format"]["schema"] == schema
    assert not _wire_tool_names(requests[0]) - {"update_plan", "request_user_input"}


async def test_native_warm_resume_replaces_gateway_without_restarting_client(tmp_path, monkeypatch, endpoint):
    """固定 CLI が同じ process で次 Attempt の新 MCP server と監査権を使用する。"""
    server, requests = endpoint
    _local_client(monkeypatch, server)
    factory = codex_engine.create_codex_client
    clients = []

    def capture(configuration):
        """実 process の生成数だけを記録し、model 通信は原 loopback のままにする。"""
        client = factory(configuration)
        clients.append(client)
        return client

    monkeypatch.setattr(codex_engine, "create_codex_client", capture)
    provider, writer, backend = CsvIssueProvider(), MemoryAuditWriter(), MemoryTranscriptBackend()
    registry = _registry(provider)
    context = replace(context_with_brief(tmp_path), model="gpt-5.6-terra")
    engine = CodexAgentSdkEngine(
        configuration=CodexRuntimeConfiguration("gpt-5.6-terra", "max", tmp_path / "codex"),
        runtime_factory=lambda run: registry.build_gateway_runtime(run, audit_writer=writer),
        transcript_backend=backend,
    )
    async with asyncio.timeout(50), engine.continuation_scope(context.run_id):
        first = [event async for event in engine.execute(context)]
        assert first[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in first]
        session = AgentSessionRef(context.run_id, context.run_attempt_id, first[0].agent_session_id)
        next_brief = deepcopy(context.task_brief)
        next_brief["identity"]["segment_no"] = 2
        next_brief["checkpoint"]["summary"] = "Continue without repeating the saved call."
        following = compiled(replace(context, run_attempt_id=uuid4()), next_brief)
        second = [event async for event in engine.resume(ResumeContext(following, session))]
        assert second[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in second]
        assert second[0].agent_session_id == session.session_id
        assert len(clients) == 1
        assert provider.calls == 2 and len(writer.completed) == 2
        assert [c.run_attempt_id for c in provider.contexts] == [context.run_attempt_id, following.run_attempt_id]
        assert len(requests) == 4
    assert not engine._active
    assert clients[0]._proc is None


async def test_native_inline_effect_returns_receipt_without_interrupting_turn(tmp_path, monkeypatch, endpoint):
    """固定 native SDK が一つの turn 内で原回执を受取り、次のモデル応答へ進む。"""
    from skillmind.effects.inline import InlineEffectResult
    from tests.runs.test_effect_continuation import receipt
    server, requests = endpoint
    _local_client(monkeypatch, server)
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    registry = ToolRegistry((_change_propose_tool_definition(ContractStore(contracts)),))
    base = _context(tmp_path, _registry(CsvIssueProvider()))
    context = replace(base, model="gpt-5.6-terra",
        tools=(registry.resolve_unbound("change.propose/v1", execution_profile="GUIDED"),),
        permission_snapshot={"allowed_capabilities":["change.propose/v1"]})
    backend = MemoryTranscriptBackend()
    calls = []
    async def complete(arguments, call_id, session_id):
        """このテストは native 接線のみ。DB/apply の保証は repository/Provider 回帰で確認する。"""
        calls.append((call_id,session_id))
        return InlineEffectResult(uuid4(),receipt())
    def runtime(run):
        """各 Run にだけ completion callback を配線する。"""
        value=registry.build_gateway_runtime(run,audit_writer=MemoryAuditWriter())
        return replace(value,mcp=replace(value.mcp,on_inline_effect=complete))
    engine=CodexAgentSdkEngine(configuration=CodexRuntimeConfiguration("gpt-5.6-terra","max",tmp_path/"codex"),
        runtime_factory=runtime,transcript_backend=backend)
    async with asyncio.timeout(30):
        events=[event async for event in engine.execute(context)]
    assert events[-1].event_type is AgentEventType.RESULT_COMPLETED, [e.payload for e in events]
    assert sum(e.event_type is AgentEventType.SESSION_STARTED for e in events)==1
    assert not any(e.event_type is AgentEventType.CHANGE_PROPOSED for e in events)
    assert any(e.event_type is AgentEventType.EFFECT_APPLIED for e in events)
    assert len(calls)==1 and len(requests)==2
