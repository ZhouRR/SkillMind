"""Skill 解釈の model transport も、主 Run と同じ同梱 CLI を使うことを確認する。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from skillmind.agent import interpreter_completion
from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.claude_build import bundled_claude_build
from tests.agent.test_claude_engine import _result


@pytest.mark.asyncio
async def test_interpreter_explicitly_selects_verified_bundle(monkeypatch):
    """query は fake とし、構造化出力と実 options の CLI path を検証する。"""
    seen = []

    async def query(*, prompt, options):
        """model/ネットワークを起動せず、呼出しに渡された options を採取する。"""
        seen.append(options)
        yield _result(str(uuid4()))

    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    result = await client.complete(
        system_prompt="Fixture",
        user_message="Fixture input",
        response_schema={"type": "object"},
        model="claude-test",
        parameters={},
    )
    assert result.structured_output == {"summary": "ok"}
    assert len(seen) == 1 and seen[0].cli_path == bundled_claude_build().cli_path


@pytest.mark.asyncio
async def test_interpreter_refuses_unavailable_bundle_before_query(monkeypatch):
    """CLI がないときに通常の SDK 探索へ戻って課金を始めない。"""

    def missing():
        """install 破損を、実 package を変更せず注入する。"""
        raise RuntimeError("Pinned runtime unavailable")

    def query(**kwargs):
        """拒否後に query を一度でも作成すると失敗する。"""
        raise AssertionError("Unexpected model query")

    monkeypatch.setattr(interpreter_completion, "bundled_claude_build", missing)
    monkeypatch.setattr(interpreter_completion, "query", query)
    client = interpreter_completion.ClaudeCompletionClient(ClaudeRuntimeConfiguration({}))
    with pytest.raises(RuntimeError, match="Pinned runtime"):
        await client.complete(
            system_prompt="Fixture",
            user_message="Fixture input",
            response_schema={"type": "object"},
            model="claude-test",
            parameters={},
        )
