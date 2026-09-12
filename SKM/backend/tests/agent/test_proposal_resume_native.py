"""固定 CLI と実 MCP に合成 loopback API を接ぎ、原 Proposal の続行を検証する。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from claude_agent_sdk.types import SessionStore

from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.context_builder import ContractStore, _change_propose_tool_definition
from skillmind.agent.domain import AgentEventType, AgentSessionRef, ResumeContext
from skillmind.agent.engine import ClaudeAgentSdkEngine
from skillmind.agent.tool_gateway import ToolRegistry
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.proposal_continuation import ResolvedProposal
from tests.agent.test_claude_agent_sdk import _run_context
from tests.agent.test_tool_gateway import MemoryAuditWriter

pytestmark = pytest.mark.skipif(
    os.environ.get("SKILLMIND_NATIVE_CLI_TESTS") != "1",
    reason="Explicit local CLI/loopback test; no external model or business database",
)
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


class MemorySessionStore(SessionStore):
    """SDK の opaque entry を変更せず保持する合成 SessionStore。"""

    def __init__(self):
        """Session/subpath ごとの追加列を初期化する。"""
        self.entries = {}

    async def append(self, key, entries):
        """SDK が生成した entry だけを順番通り保存する。"""
        self.entries.setdefault(json.dumps(key, sort_keys=True), []).extend(entries)

    async def load(self, key):
        """元の内容をそのまま復元へ渡す。"""
        return self.entries.get(json.dumps(key, sort_keys=True))

    async def list_sessions(self, project_key):
        """試験は明示 Session ID のみを使う。"""
        return []

    async def delete(self, key):
        """試験中も原 transcript を保持する。"""
        raise AssertionError("Transcript must be retained")

    async def list_subkeys(self, key):
        """子 Session は生成しない。"""
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("next_proposal", [False, True])
async def test_native_resume_consumes_original_proposal_once(tmp_path, monkeypatch, next_proposal):
    """最新 Brief を最初の続行 turn へ渡し、別の新提案は再度 pause する。"""
    arguments = json.loads((CONTRACTS / "examples/change-propose-request.v1.json").read_text())
    requests = []
    paths = []
    initial = []
    resumed = []

    class Handler(BaseHTTPRequestHandler):
        """Anthropic wire 応答だけを返し、リクエスト header を記録しない。"""

        def log_message(self, *args):
            """通常アクセスログは不要。"""

        def do_POST(self):
            """初回は提案、続行は結果または別提案を返す。"""
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens":10}')
                return
            main_request = any(
                t["name"] == "mcp__skillmind__change_propose_v1" for t in request.get("tools", [])
            )
            if main_request:
                requests.append(request)
                paths.append(self.path)
            resumed = main_request and len(requests) > 1
            tool_turn = main_request
            content = (
                {
                    "type": "tool_use",
                    "id": "toolu_next" if resumed else "toolu_original",
                    "name": "StructuredOutput"
                    if resumed and not next_proposal
                    else "mcp__skillmind__change_propose_v1",
                    "input": (
                        {"summary": "synthetic complete"}
                        if not next_proposal
                        else {**arguments, "idempotency_key": "new-proposal"}
                    )
                    if resumed
                    else arguments,
                }
                if tool_turn
                else {"type": "text", "text": '{"summary":"synthetic complete"}'}
            )
            message = {
                "id": f"msg_{len(requests)}",
                "type": "message",
                "role": "assistant",
                "model": request["model"],
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            }
            block = {**content, "input": {}} if tool_turn else {**content, "text": ""}
            delta = (
                {"type": "input_json_delta", "partial_json": json.dumps(content["input"])}
                if tool_turn
                else {"type": "text_delta", "text": content["text"]}
            )
            events = [
                {"type": "message_start", "message": message},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "tool_use" if tool_turn else "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 10},
                },
                {"type": "message_stop"},
            ]
            self.send_response(200)
            if not request.get("stream"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {**message, "content": [content], "stop_reason": "end_turn"}
                    ).encode()
                )
                return
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(
                    ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                )
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    registry = ToolRegistry((_change_propose_tool_definition(ContractStore(CONTRACTS)),))
    registered = registry.resolve("change.propose/v1", provider="platform", integration_id=None)
    base = _run_context(tmp_path)
    for directory in (base.workspace.cwd, base.workspace.input_dir, base.workspace.output_dir):
        directory.mkdir(parents=True)
    context = replace(
        base,
        tools=(registered,),
        model="claude-sonnet-4-6",
        permission_snapshot={
            "mode": "auto_read_only",
            "allowed_capabilities": ["change.propose/v1"],
        },
        result_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        task_brief_checksum="sha256:" + "1" * 64,
    )
    audits = []

    def runtime(run):
        """本番 registry/Gateway と各 Attempt の合成 audit storage を結ぶ。"""
        audit = MemoryAuditWriter()
        audits.append(audit)
        return registry.build_runtime(run, audit_writer=audit)

    engine = ClaudeAgentSdkEngine(
        mcp_server_factory=runtime,
        session_store=MemorySessionStore(),
        configuration=ClaudeRuntimeConfiguration(
            environment={
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "ANTHROPIC_API_KEY": "synthetic",
                "ANTHROPIC_AUTH_TOKEN": "synthetic",
            }
        ),
    )
    try:
        async with asyncio.timeout(40):
            initial = [event async for event in engine.execute(context)]
            assert initial[-1].event_type is AgentEventType.CHANGE_PROPOSED
            sdk_id = initial[-1].agent_session_id
            continuation = replace(
                context,
                run_attempt_id=uuid4(),
                prompt=(
                    "Verified synthetic receipt: first INSERT was APPLIED; "
                    "continue with document conversion."
                ),
                resolved_proposal=ResolvedProposal(
                    sha256_hex(canonical_json(arguments)), sdk_id, "cp_synthetic", "APPLIED"
                ),
            )
            resumed = [
                event
                async for event in engine.resume(
                    ResumeContext(
                        run=continuation,
                        session=AgentSessionRef(context.run_id, context.run_attempt_id, sdk_id),
                        input_text=continuation.prompt,
                    )
                )
            ]
            assert resumed[-1].event_type is (
                AgentEventType.CHANGE_PROPOSED if next_proposal else AgentEventType.RESULT_COMPLETED
            )
        assert len(requests) == 2
        replies = [
            block
            for message in requests[1]["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if block.get("type") == "tool_result"
        ]
        assert len(replies) == 1, {
            "messages": requests[1]["messages"],
            "audits": [len(a.completed) for a in audits],
            "events": [(e.event_type.value, dict(e.payload)) for e in resumed],
        }
        assert replies[0]["tool_use_id"] == "toolu_original"
        assert continuation.prompt in json.dumps(replies[0], ensure_ascii=False)
        assert not audits[0].completed
        assert len(audits[1].completed) == 1
        assert not audits[1].failed
        if not next_proposal:
            retry = replace(continuation, run_attempt_id=uuid4())
            async with asyncio.timeout(40):
                repeated = [
                    event
                    async for event in engine.resume(
                        ResumeContext(
                            run=retry,
                            session=AgentSessionRef(
                                context.run_id, continuation.run_attempt_id, sdk_id
                            ),
                            input_text="Continue from the persisted reply without applying again.",
                        )
                    )
                ]
            assert repeated[-1].event_type is AgentEventType.RESULT_COMPLETED
            assert len(requests) == 3
            assert not audits[2].completed
    finally:
        (tmp_path / "native-diagnostic.json").write_text(
            json.dumps(
                {
                    "paths": paths,
                    "requests": requests,
                    "events": [(e.event_type.value, dict(e.payload)) for e in initial + resumed],
                    "audits": [len(a.completed) for a in audits],
                },
                default=str,
            )
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
