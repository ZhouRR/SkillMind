"""SDK 更新時に依存宣言、生成 lock と実行時の固定 version が分離しないことを検証する。"""

from __future__ import annotations

import tomllib
from pathlib import Path

from skillmind.agent.codex_runtime import CODEX_CLI_VERSION, CODEX_SDK_VERSION


def test_codex_dependency_pins_match_runtime_versions() -> None:
    """両 lock の SDK/CLI と配備宣言を照合し、片側だけの更新を検出する。"""

    backend = Path(__file__).resolve().parents[2]
    project = tomllib.loads((backend / "pyproject.toml").read_text())
    sdk_pin = f"openai-codex=={CODEX_SDK_VERSION}"
    cli_pin = f"openai-codex-cli-bin=={CODEX_CLI_VERSION}"
    assert sdk_pin in project["project"]["dependencies"]
    for name in ("requirements.lock", "dev-requirements.lock"):
        requirements = (backend / name).read_text().splitlines()
        assert sdk_pin in requirements
        assert cli_pin in requirements
