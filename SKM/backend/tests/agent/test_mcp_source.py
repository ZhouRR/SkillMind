"""本物の MCP SDK と fake HTTP server を組み合わせ、通信境界と取消を検証する。"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from skillmind.agent import mcp_source
from skillmind.agent.mcp_source import (
    MAX_MCP_BYTES,
    BoundedMcpTransport,
    McpReadError,
    StreamableHttpMcpSource,
    validate_mcp_contents,
)

URI = "resource://reports/current"
ENDPOINT = "https://mcp.example.test/mcp"
CONFIG = {"server_url": ENDPOINT, "transport": "streamable_http"}


def json_response(value, headers=None):
    """実 HTTP と同様に未読 stream を返し、httpx の読込済み content cache を使わない。"""
    return httpx.Response(
        200,
        headers={"content-type": "application/json", **(headers or {})},
        stream=httpx.ByteStream(json.dumps(value).encode()),
    )


class ResourceStream(httpx.AsyncByteStream):
    """SSE 内容と停止状態を観測し、server request 応答待ちも再現する。"""

    def __init__(self, response, server_request=None, answered=None, waiting=False):
        """一つの resource 結果と任意の callback handshake を保持する。"""
        self.response = response
        self.server_request = server_request
        self.answered = answered
        self.waiting = waiting
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        """任意の server request が拒否されるまで read 結果を保留する。"""
        self.started.set()
        if self.server_request is not None:
            yield ("event: message\ndata: " + json.dumps(self.server_request) + "\n\n").encode()
            await self.answered.wait()
        if self.waiting:
            await asyncio.Event().wait()
        yield ("event: message\ndata: " + json.dumps(self.response) + "\n\n").encode()

    async def aclose(self):
        """client が stream を閉じた事実だけを記録する。"""
        self.closed = True


class FakeMcpServer(httpx.AsyncBaseTransport):
    """HTTP socket を開かず、SDK が送る protocol messages を記録する server。"""

    def __init__(self, *, sse=False, content=None, server_request=None, waiting=False):
        """read 内容と特殊 server 動作をテストごとに注入する。"""
        self.sse = sse
        self.content = content if content is not None else {"uri": URI, "text": "Example"}
        self.server_request = server_request
        self.waiting = waiting
        self.requests = []
        self.messages = []
        self.closed = False
        self.stream = None
        self.answered = asyncio.Event()
        self.read_started = asyncio.Event()

    async def handle_async_request(self, request):
        """初期化、通知、read、任意 GET と session DELETE にだけ応答する。"""
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(204)
        message = json.loads(request.content)
        self.messages.append(message)
        method = message.get("method")
        if method == "initialize":
            return json_response(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {"resources": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                        "instructions": "Private server instructions are not model guidance",
                    },
                },
                headers={"Mcp-Session-Id": "fixture-session"},
            )
        if method == "resources/read":
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"contents": [self.content]},
            }
            self.read_started.set()
            if self.sse:
                self.stream = ResourceStream(
                    response, self.server_request, self.answered, self.waiting
                )
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=self.stream
                )
            return json_response(response)
        if method is None:
            self.answered.set()
        return httpx.Response(202)

    async def aclose(self):
        """専用 client の close を観測する。"""
        self.closed = True


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("token", [None, "fixture-token"])
@pytest.mark.asyncio
async def test_sdk_reads_json_and_sse_with_optional_token_and_closes_session(sse, token):
    """SDK の handshake と resource read を実行し、固定宛先/credential/close を確認する。"""
    server = FakeMcpServer(sse=sse)
    result = await StreamableHttpMcpSource(transport_factory=lambda: server).read(
        CONFIG, token, URI
    )
    assert result == ({"uri": URI, "text": "Example"},)
    assert server.closed and any(request.method == "DELETE" for request in server.requests)
    assert [message.get("method") for message in server.messages] == [
        "initialize",
        "notifications/initialized",
        "resources/read",
    ]
    assert server.messages[0]["params"]["capabilities"] == {}
    for request in server.requests:
        assert str(request.url) == ENDPOINT
        assert request.headers.get("authorization") == (f"Bearer {token}" if token else None)
        assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "content",
    [
        {"uri": "resource://private/other", "text": "Outside scope"},
        {"uri": URI, "blob": "invalid base64"},
        {"uri": URI, "text": "Example", "blob": "YQ=="},
        {"uri": URI, "text": "a" * MAX_MCP_BYTES},
    ],
)
@pytest.mark.asyncio
async def test_sdk_rejects_other_uris_invalid_blobs_and_oversized_wire(content):
    """遠端内容を反射せず、失敗時も transport を閉じる。"""
    server = FakeMcpServer(content=content)
    with pytest.raises(McpReadError, match="could not be completed"):
        await StreamableHttpMcpSource(transport_factory=lambda: server).read(CONFIG, None, URI)
    assert server.closed


@pytest.mark.parametrize(
    "method,params",
    [
        ("roots/list", {}),
        (
            "sampling/createMessage",
            {
                "messages": [{"role": "user", "content": {"type": "text", "text": "Example"}}],
                "maxTokens": 1,
            },
        ),
        (
            "elicitation/create",
            {"message": "Example", "requestedSchema": {"type": "object", "properties": {}}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_remote_requests_cannot_open_sampling_elicitation_or_roots(method, params):
    """remote callback 要求は SDK で拒否し、resource の本文以外を結果に含めない。"""
    server = FakeMcpServer(
        sse=True,
        server_request={"jsonrpc": "2.0", "id": "server-1", "method": method, "params": params},
    )
    result = await StreamableHttpMcpSource(transport_factory=lambda: server).read(CONFIG, None, URI)
    reply = next(message for message in server.messages if message.get("id") == "server-1")
    assert "error" in reply and "result" not in reply
    assert result == ({"uri": URI, "text": "Example"},)


@pytest.mark.asyncio
async def test_cancellation_closes_owned_stream_and_client_without_retry():
    """SSE 待機中の取消は上へ伝播し、バックグラウンド読取を残さない。"""
    server = FakeMcpServer(sse=True, waiting=True)
    task = asyncio.create_task(
        StreamableHttpMcpSource(transport_factory=lambda: server).read(CONFIG, None, URI)
    )
    await asyncio.wait_for(server.read_started.wait(), timeout=2)
    await asyncio.wait_for(server.stream.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert server.closed and server.stream.closed
    assert sum(message.get("method") == "resources/read" for message in server.messages) == 1


@pytest.mark.parametrize(
    "headers,status",
    [({"location": ENDPOINT + "/other"}, 307), ({"content-encoding": "gzip"}, 200)],
)
@pytest.mark.asyncio
async def test_transport_rejects_redirect_and_compression(headers, status):
    """SDK の origin 内 redirect も含め、指定 endpoint 以外に資格情報を送らない。"""
    calls = []

    def respond(request):
        """一回だけ HTTP 応答を返す。"""
        calls.append(request)
        return httpx.Response(status, headers=headers)

    transport = BoundedMcpTransport(ENDPOINT, URI, httpx.MockTransport(respond))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(McpReadError):
            await client.get(ENDPOINT)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "uri,payload", [(URI, {"text": "text", "blob": "YQ=="}), ("resource://other", {"text": "text"})]
)
def test_injected_source_cannot_bypass_content_validation(uri, payload):
    """Provider の注入 port にも同じ content 境界を適用する。"""
    with pytest.raises(McpReadError):
        validate_mcp_contents(({"uri": uri, **payload},), URI)


@pytest.mark.asyncio
async def test_read_deadline_covers_silent_resource_and_closes_client(monkeypatch):
    """通知を出さない server も呼出し全体の deadline で終了する。"""
    monkeypatch.setattr(mcp_source, "_READ_TIMEOUT_SECONDS", 0.05)
    server = FakeMcpServer(sse=True, waiting=True)
    with pytest.raises(McpReadError):
        await StreamableHttpMcpSource(transport_factory=lambda: server).read(CONFIG, None, URI)
    assert server.closed and server.stream.closed


@pytest.mark.asyncio
async def test_http_request_count_is_bounded_before_io():
    """SDK が再接続しても、同じ invocation で新しい回数予算を取得しない。"""
    calls = []

    def respond(request):
        """接続の実回数を観測する。"""
        calls.append(request)
        return httpx.Response(405)

    async with httpx.AsyncClient(
        transport=BoundedMcpTransport(ENDPOINT, URI, httpx.MockTransport(respond))
    ) as client:
        for _ in range(16):
            await client.get(ENDPOINT)
        with pytest.raises(McpReadError):
            await client.get(ENDPOINT)
    assert len(calls) == 16


@pytest.mark.asyncio
async def test_multiple_responses_share_wire_budget():
    """個別に小さい SSE/JSON 応答でも全接続合計の上限を超えられない。"""
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=httpx.ByteStream(b"a" * MAX_MCP_BYTES))
    )
    async with httpx.AsyncClient(transport=BoundedMcpTransport(ENDPOINT, URI, transport)) as client:
        for _ in range(3):
            await client.get(ENDPOINT)
        with pytest.raises(McpReadError):
            await client.get(ENDPOINT)


@pytest.mark.asyncio
@pytest.mark.parametrize("json_response", [False, True])
async def test_real_loopback_http_with_isolated_sdk_server(json_response, monkeypatch):
    """合成データのローカル server で実 socket と SDK 間の JSON/SSE 相互運用を確認する。"""
    app = FastMCP("isolated-read-test", stateless_http=True, json_response=json_response)

    @app.resource(URI)
    def report() -> str:
        """外部データを使わない合成 resource。"""
        return "Example loopback report"

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app.streamable_http_app(),
                log_config=None,
                access_log=False,
                lifespan="on",
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        raise AssertionError("Loopback server did not start")
                    await asyncio.sleep(0.01)
            result = await StreamableHttpMcpSource().read(
                {"server_url": f"http://127.0.0.1:{port}/mcp", "transport": "streamable_http"},
                None,
                URI,
            )
            assert result == (
                {"uri": URI, "mime_type": "text/plain", "text": "Example loopback report"},
            )
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=5)
