"""固定した実 CLI の turns/再開を、合成 loopback API で観測する。実モデルは使わない。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

_CHILD_KEYS = {
    "PATH",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "LC_CTYPE",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}


class FixtureApi:
    """受信本文を保存せず、原 scenario の要求数と合成応答だけを管理する。"""

    def __init__(self) -> None:
        """各 probe に独立した観測を作る。"""
        self.scenario = "initial"
        self.loop = False
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.tool_calls: list[str] = []

    def reply(self, path: str, body: dict[str, Any]) -> tuple[bytes, str]:
        """既知の Messages API の形だけ受理し、未知の接続先へ転送しない。"""
        if path != "/v1/messages" or body.get("model") != "claude-sonnet-4-6":
            raise ValueError("Unexpected fixture API request")
        self.requests.append({"scenario": self.scenario, "messages": len(body["messages"])})
        content = (
            {
                "type": "tool_use",
                "id": f"tool_fixture_{len(self.requests)}",
                "name": "mcp__fixture__step",
                "input": {},
            }
            if self.loop
            else {"type": "text", "text": "Fixture complete."}
        )
        if self.scenario in {"structured-valid", "structured-invalid"}:
            content = {
                "type": "tool_use",
                "id": f"tool_fixture_{len(self.requests)}",
                "name": "StructuredOutput",
                "input": {"answer": "fixture" if self.scenario == "structured-valid" else 0},
            }
        is_tool = content["type"] == "tool_use"
        reason = "tool_use" if is_tool else "end_turn"
        message = {
            "id": f"msg_fixture_{len(self.requests)}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [content],
            "stop_reason": reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
        if not body.get("stream"):
            return json.dumps(message).encode(), "application/json"
        events = [
            {
                "type": "message_start",
                "message": {
                    **message,
                    "content": [],
                    "stop_reason": None,
                    "usage": {**message["usage"], "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {**content, "input": {}}
                if is_tool
                else {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(content["input"])}
                if is_tool
                else {"type": "text_delta", "text": "Fixture complete."},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": reason, "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        return "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ).encode(), "text/event-stream"


def handler(api: FixtureApi) -> type[BaseHTTPRequestHandler]:
    """外部転送を持たず、loopback の認証済み合成要求だけ処理する handler を作る。"""

    class Handler(BaseHTTPRequestHandler):
        """既定 access log と原 request/body の出力を止める。"""

        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            """公開するのは構造化した観測結果だけとする。"""

        def do_CONNECT(self) -> None:
            """Proxy 経由の外向き接続を閉じ、試行自体も probe の失敗にする。"""
            api.errors.append("Unexpected CONNECT")
            self.send_error(403)

        def do_GET(self) -> None:
            """付随 discovery/telemetry は合成 API の成功へ混ぜない。"""
            api.errors.append("Unexpected GET")
            self.send_error(404)

        def do_POST(self) -> None:
            """入力サイズ・認証・path を確認してから固定応答を返す。"""
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4 * 1024 * 1024:
                    raise ValueError("Invalid fixture request size")
                if self.headers.get("x-api-key") != "fixture-only":
                    raise ValueError("Unexpected fixture authentication")
                address = urlsplit(self.path)
                if address.scheme or address.netloc:
                    raise ValueError("Proxy forwarding is prohibited")
                body = json.loads(self.rfile.read(size))
                payload, mime = api.reply(address.path, body)
            except (ValueError, KeyError, TypeError) as error:
                api.errors.append(type(error).__name__)
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

    return Handler


async def probe() -> dict[str, Any]:
    """隔離した子 process だけで SDK/CLI を import して起動する。"""
    if (
        set(os.environ) - _CHILD_KEYS
        or os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") != "1"
        or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
    ):
        raise RuntimeError("The probe requires its isolated child environment")
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        create_sdk_mcp_server,
        tool,
    )
    from claude_agent_sdk.types import ResultMessage
    from skillmind.agent.claude_build import bundled_claude_build

    build = bundled_claude_build()
    api = FixtureApi()
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="skillmind-cli-metering-") as root:
        os.environ["CLAUDE_CONFIG_DIR"] = root + "/config"
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(api))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        proxy = {
            "HTTP_PROXY": endpoint,
            "HTTPS_PROXY": endpoint,
            "ALL_PROXY": endpoint,
            "NO_PROXY": "127.0.0.1,localhost",
        }
        os.environ.update(proxy)
        env = {
            **proxy,
            "ANTHROPIC_API_KEY": "fixture-only",
            "ANTHROPIC_AUTH_TOKEN": "",
            "ANTHROPIC_BASE_URL": endpoint,
            "CLAUDE_CONFIG_DIR": root + "/config",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        options = ClaudeAgentOptions(
            cli_path=build.cli_path,
            cwd=root,
            env=env,
            system_prompt="Synthetic local test.",
            model="claude-sonnet-4-6",
            tools=[],
            skills=[],
            allowed_tools=[],
            disallowed_tools=["Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch"],
            strict_mcp_config=True,
            mcp_servers={},
            setting_sources=[],
            max_turns=3,
            session_id=str(uuid4()),
        )

        @tool("step", "Return a synthetic fixture.", {"type": "object", "properties": {}})
        async def step(_args: Any) -> dict[str, Any]:
            """副作用のない応答と呼出し数だけを返す。"""
            api.tool_calls.append(api.scenario)
            return {"content": [{"type": "text", "text": "fixture step complete"}]}

        async def receive(client: ClaudeSDKClient, scenario: str) -> dict[str, Any]:
            """一 query の Result を一件だけ採取し、float は生の hex として保存する。"""
            api.scenario = scenario
            before = len(api.requests)
            await client.query("Synthetic probe query.")
            results = []
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    results.append(message)
            if len(results) != 1:
                raise RuntimeError("Expected one fixture Result per query")
            result = results[0]
            if scenario == "structured-valid":
                assert result.structured_output == {"answer": "fixture"}
            record = {
                "scenario": scenario,
                "requests": len(api.requests) - before,
                "num_turns": result.num_turns,
                "subtype": result.subtype,
                "is_error": result.is_error,
                "stop_reason": result.stop_reason,
                "has_structured_output": result.structured_output is not None,
                "cost_binary64": result.total_cost_usd.hex()
                if isinstance(result.total_cost_usd, float)
                else None,
            }
            records.append(record)
            return record

        try:
            async with asyncio.timeout(90):
                async with ClaudeSDKClient(options) as client:
                    first = await receive(client, "initial")
                    same = await receive(client, "same-client")
                resumed = replace(options, resume=options.session_id, session_id=None)
                async with ClaudeSDKClient(resumed) as client:
                    resume = await receive(client, "resume")
                forked = replace(resumed, session_id=str(uuid4()), fork_session=True)
                async with ClaudeSDKClient(forked) as client:
                    fork = await receive(client, "fork")
                for scenario in ("structured-text-only", "structured-valid", "structured-invalid"):
                    structured = replace(
                        options,
                        session_id=str(uuid4()),
                        max_turns=2,
                        output_format={
                            "type": "json_schema",
                            "schema": {
                                "type": "object",
                                "properties": {"answer": {"type": "string"}},
                                "required": ["answer"],
                                "additionalProperties": False,
                            },
                        },
                    )
                    async with ClaudeSDKClient(structured) as client:
                        result = await receive(client, scenario)
                    expected = {
                        "structured-text-only": (2, 2, "success", False, False, "end_turn"),
                        "structured-valid": (1, 2, "success", False, True, "tool_use"),
                        "structured-invalid": (2, 3, "error_max_turns", True, False, "tool_use"),
                    }[scenario]
                    assert (
                        tuple(
                            result[key]
                            for key in (
                                "requests",
                                "num_turns",
                                "subtype",
                                "is_error",
                                "has_structured_output",
                                "stop_reason",
                            )
                        )
                        == expected
                    )
                api.loop = True
                for limit in (1, 2, 3):
                    limited = replace(
                        options,
                        session_id=str(uuid4()),
                        max_turns=limit,
                        mcp_servers={
                            "fixture": create_sdk_mcp_server(name="fixture", tools=[step])
                        },
                        allowed_tools=["mcp__fixture__step"],
                    )
                    async with ClaudeSDKClient(limited) as client:
                        result = await receive(client, f"limit-{limit}")
                    assert result["requests"] == limit and result["num_turns"] == limit + 1
                    assert result["subtype"] == "error_max_turns" and result["is_error"]
                    assert api.tool_calls.count(f"limit-{limit}") == limit
                assert all(
                    r["requests"] == r["num_turns"] == 1 for r in (first, same, resume, fork)
                )
                assert all(
                    r["subtype"] == "success"
                    and not r["is_error"]
                    and r["stop_reason"] == "end_turn"
                    for r in (first, same, resume, fork)
                )
                # Session の再利用を指定しただけでなく、履歴が次の要求へ届くことを確認する。
                assert [row["messages"] for row in api.requests[:4]] == [1, 3, 5, 7]
                assert float.fromhex(same["cost_binary64"]) == 2 * float.fromhex(
                    first["cost_binary64"]
                )
                assert first["cost_binary64"] == resume["cost_binary64"] == fork["cost_binary64"]
                assert not api.errors, api.errors
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    return {
        "sdk_version": build.sdk_version,
        "cli_version": build.cli_version,
        "cli_checksum": build.cli_checksum,
        "system": platform.system(),
        "machine": platform.machine(),
        "api": "synthetic-loopback",
        "records": records,
        "request_history_lengths": [row["messages"] for row in api.requests],
    }


def main() -> None:
    """実行環境を子へ移さず、期限超過時は所有する process group 全体を止める。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--isolated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("This process-lifecycle probe currently supports Linux only")
    if args.isolated:
        print(json.dumps(asyncio.run(probe()), sort_keys=True))
        return
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--isolated"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=120)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"Local CLI probe failed: {stderr[-4000:]}")
    report = json.loads(stdout)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
