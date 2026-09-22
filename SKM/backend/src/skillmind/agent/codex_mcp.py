"""Codex へ Run 専用 MCP を loopback で公開し、既存 Gateway の認可を再利用する。"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from copy import deepcopy
from typing import Any
from uuid import uuid4

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from skillmind.agent.domain import AgentEventType, RunContext
from skillmind.agent.tool_gateway import RunToolRuntime
from skillmind.agent.tool_policy import ToolExecutionPolicy
from skillmind.effects.inline import inline_success

BridgeEvent = tuple[AgentEventType, dict[str, Any]]
DeferredHandler = Callable[[str, Mapping[str, Any], str, str], Awaitable[None]]
_PREFIX = "mcp__skillmind__"


class CodexToolBridge:
    """Thread identity と一度だけの停止要求を持つ、Attempt 専用 Tool 実行境界。"""

    def __init__(
        self,
        context: RunContext,
        runtime: RunToolRuntime,
        *,
        on_deferred: DeferredHandler,
    ) -> None:
        """許可済み Schema/Provider と制御 Tool の永続保存 callback を保持する。"""

        self.context = context
        self.runtime = runtime
        self.session_id: str | None = None
        self.events: asyncio.Queue[BridgeEvent] = asyncio.Queue()
        self.deferred: BridgeEvent | None = None
        self.stop_reason: str | None = None
        self.parked = asyncio.Event()
        self.closed = asyncio.Event()
        self.accepting = True
        self._calls = 0
        self._on_deferred = on_deferred
        self._lock = asyncio.Lock()
        self._identity = str(uuid4())
        self._policy = ToolExecutionPolicy(
            context.tools,
            allowed_capabilities=frozenset(
                context.permission_snapshot["allowed_capabilities"],
            ),
        )
        self.server: Server[Any, Any] = Server("skillmind")

        # 固定 MCP SDK の decorator は入力関数の型を公開していない。
        @self.server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
        async def list_tools() -> list[Tool]:
            """Capability 名と登録済み Schema だけを公開する。"""

            return [
                Tool(
                    name=tool.sdk_name.removeprefix(_PREFIX),
                    description=runtime.tool_descriptions.get(tool.sdk_name, tool.capability),
                    inputSchema=dict(tool.input_schema),
                )
                for tool in self._policy.sdk_tools
            ]

        @self.server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
            """MCP request ID を原呼出し ID とし、Gateway へ一度だけ渡す。"""

            request_id = str(self.server.request_context.request_id)
            return await self.invoke(
                _PREFIX + name, arguments, f"mcp:{self._identity}:{request_id}"
            )

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any],
        call_id: str,
    ) -> CallToolResult:
        """停止後の新規 Tool を拒否し、認可/監査/Provider 完了を同一所有範囲で待つ。"""

        arguments = deepcopy(dict(arguments))
        async with self._lock:
            if not self.accepting or self.session_id is None:
                return _tool_error("Tool execution is stopped")
            if self._calls >= self.context.limits.max_turns:
                self.accepting = False
                self.stop_reason = "run_limit_exceeded"
                self.parked.set()
                return _tool_error("Run tool step limit exceeded")
            self._calls += 1
            try:
                if self.runtime.mcp.on_tool_attempt is not None:
                    self.runtime.mcp.on_tool_attempt()
                self._policy.authorize(name, arguments)
            except PermissionError as error:
                if self.runtime.mcp.on_tool_denied is not None:
                    await self.runtime.mcp.on_tool_denied(
                        name,
                        arguments,
                        call_id,
                        self.session_id,
                        "Tool policy denied",
                    )
                return _tool_error(str(error))
            await self.events.put(
                (
                    AgentEventType.TOOL_REQUESTED,
                    {
                        "tool_use_id": call_id,
                        "tool_name": name,
                        "input_keys": sorted(arguments),
                    },
                )
            )
            if name in self.runtime.mcp.deferred_tool_names:
                # Provider を呼ばない。原要求を保存してから native turn を停止し、Worker が
                # 通常の parse/transaction で Interaction/Proposal を作成する。
                await self._on_deferred(name, arguments, call_id, self.session_id)
                inline_proposal_id = None
                if (name == _PREFIX + "change_propose_v1"
                    and self.runtime.mcp.on_inline_effect is not None):
                    try:
                        inline = await self.runtime.mcp.on_inline_effect(arguments, call_id, self.session_id)
                    except ValueError:
                        return _tool_error("Controlled operation does not match its frozen contract or scope.")
                    if inline is not None:
                        inline_proposal_id = str(inline.proposal_id)
                        if inline.receipt is not None:
                            # 事実は DB に確定済み。Engine が順序を採番して通常 event を配送する。
                            await self.events.put((AgentEventType.EFFECT_APPLIED, {
                                "proposal_id": inline_proposal_id,
                                "proposal_ref": inline.receipt["proposal_ref"],
                                "effect_execution_id": inline.receipt["effect_execution_id"],
                                "status": "APPLIED", "before_ref": inline.receipt["before_ref"],
                                "after_ref": inline.receipt["after_ref"], "delivery": "INLINE",
                            }))
                            return CallToolResult(content=[TextContent(type="text", text=json.dumps(inline_success(inline.receipt), ensure_ascii=False))])
                event_type = (
                    AgentEventType.CHANGE_PROPOSED
                    if name == _PREFIX + "change_propose_v1"
                    else AgentEventType.INTERACTION_REQUESTED
                )
                key = (
                    "change_proposal_request"
                    if event_type is AgentEventType.CHANGE_PROPOSED
                    else "interaction_request"
                )
                self.deferred = (
                    event_type,
                    {
                        key: arguments,
                        **({"inline_proposal_id": inline_proposal_id} if inline_proposal_id else {}),
                        "deferred_tool": {
                            "tool_use_id": call_id,
                            "tool_name": name,
                            "input_keys": sorted(arguments),
                        },
                    },
                )
                self.accepting = False
                self.parked.set()
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=(
                            '{"status":"paused","message":"The platform is handling this. '
                                'Stop and wait for the next user turn."}'
                            ),
                        )
                    ]
                )
            authorize = self.runtime.mcp.on_tool_authorized
            if authorize is None:
                raise RuntimeError("Codex Tool requires its authorization gateway")
            await authorize(name, arguments, call_id, self.session_id)
            result = await self.runtime.gateway.invoke_mcp(name, arguments)
            failed = bool(result.get("is_error"))
            await self.events.put(
                (
                    AgentEventType.TOOL_FAILED if failed else AgentEventType.TOOL_COMPLETED,
                    {"tool_use_id": call_id},
                )
            )
            return CallToolResult(
                content=[TextContent.model_validate(block) for block in result["content"]],
                isError=failed,
            )


def _tool_error(message: str) -> CallToolResult:
    """固定の境界診断だけを model へ返す。"""

    return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


class _OwnedServer(uvicorn.Server):
    """Worker の process signal handler を上書きしない内部 ASGI server。"""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        """停止は呼出し元の明示 lifecycle だけに従う。"""

        yield


@asynccontextmanager
async def serve_codex_tools(bridge: CodexToolBridge) -> AsyncIterator[dict[str, Any]]:
    """予測不能 URL と loopback socket を一 Attempt だけ保持し、handler の終了を待つ。"""

    manager = StreamableHTTPSessionManager(bridge.server, json_response=True, stateless=True)
    path = "/" + secrets.token_urlsafe(32)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        """非公開 path 以外は MCP の初期化や Tool 一覧へ到達させない。"""

        if scope["type"] != "http" or not secrets.compare_digest(scope["path"], path):
            await Response(status_code=404)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = _OwnedServer(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=10,
        )
    )
    try:
        async with manager.run():
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("Codex Tool server did not start")
                        await asyncio.sleep(0.01)
                yield {
                    "url": f"http://127.0.0.1:{port}{path}",
                    "required": True,
                    "default_tools_approval_mode": "approve",
                    "enabled_tools": [
                        tool.sdk_name.removeprefix(_PREFIX) for tool in bridge._policy.sdk_tools
                    ],
                    "startup_timeout_sec": 10,
                    "tool_timeout_sec": 300,
                }
            finally:
                bridge.accepting = False
                server.should_exit = True
                try:
                    async with asyncio.timeout(15):
                        await asyncio.shield(task)
                finally:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    sock.close()
    finally:
        bridge.closed.set()
