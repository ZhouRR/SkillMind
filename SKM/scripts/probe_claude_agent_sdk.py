"""固定した Claude Agent SDK/CLI の offline compatibility probe を実行する。"""

from __future__ import annotations

import json
from dataclasses import asdict

from skillmind.agent.compatibility import probe_claude_agent_sdk


def main() -> None:
    """Probe 結果を Secret を含まない JSON として標準出力へ返す。"""

    report = probe_claude_agent_sdk()
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
