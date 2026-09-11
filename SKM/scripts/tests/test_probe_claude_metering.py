"""本機 CLI probe の環境分離と期限後 cleanup を、CLI を起動せず検証する。"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from scripts import probe_claude_metering as probe


class ProbeMeteringTests(unittest.TestCase):
    """実 model/credential を持たない親 launcher と fixture 応答だけを検証する。"""

    def test_launcher_does_not_inherit_model_credentials(self) -> None:
        """SDK の version check 子 process にも、親の資格情報を渡さない。"""
        process = MagicMock(returncode=0)
        process.communicate.return_value = (
            json.dumps({"api": "synthetic-loopback"}),
            "",
        )
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-parent-credential"}),
            patch.object(probe.subprocess, "Popen", return_value=process) as launch,
            patch.object(probe.sys, "argv", ["probe"]),
            patch.object(probe.platform, "system", return_value="Linux"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            probe.main()
        environment = launch.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertEqual(environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertIn("--isolated", launch.call_args.args[0])

    def test_timeout_stops_the_owned_process_group(self) -> None:
        """子の timeout で本 probe の process group を残さず、別 PID を探索しない。"""
        process = MagicMock(pid=12345)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("fixture", 120),
            ("", ""),
        ]
        with (
            patch.object(probe.subprocess, "Popen", return_value=process),
            patch.object(probe.os, "killpg") as kill,
            patch.object(probe.sys, "argv", ["probe"]),
            patch.object(probe.platform, "system", return_value="Linux"),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            probe.main()
        kill.assert_called_once_with(12345, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_direct_child_entry_rejects_inherited_credentials(self) -> None:
        """内部 flag を直接渡しても、汚れた環境で SDK を import/起動しない。"""
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-only"}, clear=True),
            self.assertRaisesRegex(RuntimeError, "isolated child"),
        ):
            asyncio.run(probe.probe())

    def test_fixture_stream_contains_tool_stop_reason(self) -> None:
        """限額を試す入力は終了 text でなく、必ず継続 Tool 要求を返す。"""
        api = probe.FixtureApi()
        api.loop = True
        payload, mime = api.reply(
            "/v1/messages",
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
            },
        )
        self.assertEqual(mime, "text/event-stream")
        events = [
            json.loads(line[6:])
            for line in payload.decode().splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[1]["content_block"]["name"], "mcp__fixture__step")
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(api.requests, [{"scenario": "initial", "messages": 1}])


if __name__ == "__main__":
    unittest.main()
