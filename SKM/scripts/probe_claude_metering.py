"""固定した実 CLI の計量・中断清理を、合成 loopback API で観測する。実モデルは使わない。"""

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
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from claude_agent_sdk.types import ResultMessage
    from skillmind.agent.claude_build import bundled_claude_build
    from skillmind.agent.claude_client import DrainingClaudeClient

    build = bundled_claude_build()
    api = FixtureApi()
    records: list[dict[str, Any]] = []
    lifecycle = {}
    startup_lifecycle = []
    engine_results = []
    tool_entered = asyncio.Event()
    tool_cleaned = asyncio.Event()
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
            if api.scenario == "interrupt-tool":
                tool_entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.05)
                    tool_cleaned.set()
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

        async def check_startup_cleanup(cancel: bool) -> dict[str, Any]:
            """Query 作成前の実 CLI 起動を失敗/取消しで止め、元 process の終了を調べる。"""
            entered = asyncio.Event()
            process = None

            class InterruptedStartup(SubprocessCLITransport):
                """実 subprocess を作成した直後で制御を保留する合成障害点。"""

                async def connect(self) -> None:
                    """SDK が transport を所有しているが Query は未作成の窓を固定する。"""
                    nonlocal process
                    await super().connect()
                    process = self._process
                    entered.set()
                    if cancel:
                        await asyncio.Event().wait()
                    raise RuntimeError("Synthetic startup failure")

            starting_options = replace(options, session_id=str(uuid4()))
            transport = InterruptedStartup("", starting_options)
            client = DrainingClaudeClient(starting_options, transport=transport)
            connecting = asyncio.create_task(client.connect())
            requests_before = len(api.requests)
            try:
                async with asyncio.timeout(15):
                    await entered.wait()
                    if cancel:
                        connecting.cancel()
                    try:
                        await connecting
                    except asyncio.CancelledError:
                        assert cancel
                    except RuntimeError as error:
                        assert not cancel and str(error) == "Synthetic startup failure"
                    else:
                        raise AssertionError("Expected startup interruption")
                assert process is not None and process.returncode is not None
                assert client._transport is None
                assert len(api.requests) == requests_before
                return {
                    "scenario": "startup-cancel" if cancel else "startup-failure",
                    "direct_cli_returncode": process.returncode,
                    "requests": len(api.requests) - requests_before,
                }
            finally:
                if not connecting.done():
                    connecting.cancel()
                    await asyncio.gather(connecting, return_exceptions=True)
                # 検証失敗時も probe 自身が作った process だけを回収する。
                await transport.close()

        async def check_engine_output(valid: bool) -> dict[str, Any]:
            """実 Engine の生成 options/hook と既定 client を合成出力で検証する。"""
            from skillmind.agent.claude import ClaudeRuntimeConfiguration
            from skillmind.agent.domain import RunContext, RunLimits, RunWorkspace
            from skillmind.agent.engine import (
                ClaudeAgentSdkEngine,
                RunMcpRuntime,
                _default_client_factory,
            )

            workspace_root = Path(root) / ("engine-valid" if valid else "engine-invalid")
            for folder in ("workspace", "input", "output", "temp"):
                (workspace_root / folder).mkdir(parents=True, exist_ok=True)
            context = RunContext(
                run_id=uuid4(),
                run_attempt_id=uuid4(),
                project_id=uuid4(),
                user_id=uuid4(),
                prompt="Return the synthetic fixture answer.",
                task_snapshot={},
                skill_snapshots=(),
                resolved_sources={},
                permission_snapshot={"mode": "auto_read_only", "allowed_capabilities": []},
                workspace=RunWorkspace(
                    root=workspace_root,
                    cwd=workspace_root / "workspace",
                    input_dir=workspace_root / "input",
                    output_dir=workspace_root / "output",
                    temp_dir=workspace_root / "temp",
                ),
                limits=RunLimits(max_turns=2, wall_timeout_seconds=30, max_output_bytes=4096),
                result_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                tools=(),
                model="claude-sonnet-4-6",
            )
            authorized, denied = [], []

            async def record_authorized(name: str, *_args: Any) -> None:
                """結果出力を資源呼出しの監査へ流していないことを記録する。"""
                authorized.append(name)

            async def record_denied(name: str, *_args: Any) -> None:
                """資源 Tool の拒否通知だけを数え、引数本文を保存しない。"""
                denied.append(name)

            def fixture_client(actual_options: ClaudeAgentOptions):
                """合成接続先を確認し、probe 専用の拒否 proxy/通信抑止だけを加える。"""
                assert actual_options.env["ANTHROPIC_BASE_URL"] == endpoint
                actual_options.env.update(proxy)
                actual_options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
                return _default_client_factory(actual_options)

            engine = ClaudeAgentSdkEngine(
                mcp_server_factory=lambda _context: RunMcpRuntime(
                    server=create_sdk_mcp_server(name="skillmind", tools=[]),
                    on_tool_authorized=record_authorized,
                    on_tool_denied=record_denied,
                ),
                configuration=ClaudeRuntimeConfiguration(
                    environment={
                        "ANTHROPIC_API_KEY": "fixture-only",
                        "ANTHROPIC_BASE_URL": endpoint,
                    }
                ),
                client_factory=fixture_client,
            )
            api.scenario = "structured-valid" if valid else "structured-invalid"
            events = [event async for event in engine.execute(context)]
            outputs = [
                event.payload["structured_output"]
                for event in events
                if event.payload.get("structured_output") is not None
            ]
            expected = "RESULT_COMPLETED" if valid else "ENGINE_FAILED"
            assert events[-1].event_type.value == expected
            assert outputs == ([{"answer": "fixture"}] if valid else [])
            assert not authorized and not denied
            assert not any(event.event_type.value.startswith("TOOL_") for event in events)
            return {
                "scenario": "engine-valid-output" if valid else "engine-invalid-output",
                "terminal_event": expected,
                "has_structured_output": bool(outputs),
                "resource_authorizations": len(authorized),
            }

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
                cancellation_options = replace(
                    options,
                    session_id=str(uuid4()),
                    max_turns=2,
                    mcp_servers={"fixture": create_sdk_mcp_server(name="fixture", tools=[step])},
                    allowed_tools=["mcp__fixture__step"],
                )
                transport = SubprocessCLITransport("", cancellation_options)
                client = DrainingClaudeClient(cancellation_options, transport=transport)
                await client.connect()
                process = transport._process
                assert process is not None
                receiving = asyncio.create_task(receive(client, "interrupt-tool"))
                try:
                    async with asyncio.timeout(10):
                        await tool_entered.wait()
                        await client.interrupt()
                        cancelled_result = await receiving
                        await client.disconnect()
                    lifecycle = {
                        "result": cancelled_result,
                        "direct_cli_returncode": process.returncode,
                        "tool_cleaned_at_disconnect": tool_cleaned.is_set(),
                    }
                    try:
                        async with asyncio.timeout(1):
                            await tool_cleaned.wait()
                    except TimeoutError:
                        pass
                    lifecycle["tool_cleaned_after_wait"] = tool_cleaned.is_set()
                    assert lifecycle["direct_cli_returncode"] is not None
                    assert lifecycle["tool_cleaned_at_disconnect"] is True
                finally:
                    if not receiving.done():
                        receiving.cancel()
                        await asyncio.gather(receiving, return_exceptions=True)
                    await client.disconnect()
                for cancel in (False, True):
                    startup_lifecycle.append(await check_startup_cleanup(cancel))
                for valid in (True, False):
                    engine_results.append(await check_engine_output(valid))
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
        "lifecycle": lifecycle,
        "startup_lifecycle": startup_lifecycle,
        "engine_results": engine_results,
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
