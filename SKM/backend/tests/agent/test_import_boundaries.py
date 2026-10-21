"""共有契約と業務 module を、モデル SDK の導入なしで独立して読み込める。"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "skillmind.agent.domain", "skillmind.agent.tool_gateway", "skillmind.runs.repository",
    "skillmind.effects.service", "skillmind.documents.service", "skillmind.skills.service",
])
def test_business_imports_do_not_load_agent_sdks(module: str) -> None:
    """既存 interpreter の import cache に隠さず、新 process で SDK 読込を禁止する。"""
    source = """
import importlib.abc
import sys
class DenySdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'claude_agent_sdk', 'openai_codex'}:
            raise AssertionError('Business import attempted an Agent SDK: ' + fullname)
sys.meta_path.insert(0, DenySdk())
__import__(sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", source, module], capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_codex_adapter_does_not_import_claude_sdk() -> None:
    """共通の会話保存型を読むだけで、他方の SDK を必要としない。"""
    source = """
import importlib.abc
import sys
class DenyClaude(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'claude_agent_sdk':
            raise AssertionError('Codex adapter attempted to import Claude SDK')
sys.meta_path.insert(0, DenyClaude())
import skillmind.agent.codex_engine
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True,
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
