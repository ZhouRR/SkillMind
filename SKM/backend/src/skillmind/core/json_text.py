"""Model 出力 text から JSON を取り出す共通前処理を一箇所に固定する。"""

from __future__ import annotations

import re

_CODE_FENCE_PATTERN = re.compile(
    r"^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?\s*```\s*$", re.DOTALL
)


def strip_code_fence(text: str) -> str:
    """全体を単一の Markdown code fence が包む場合だけ、決定的に中身を返す。

    Prompt 契約に反して model が JSON を単一の code fence (```json ... ```) で包む場合が
    あるため、fence の除去だけは決定的な前処理として許可する。Markdown 本文の推測変換は
    行わない。task-run と Skill 解釈の両 decode で同じ実装を共有する。
    """

    match = _CODE_FENCE_PATTERN.match(text)
    return match.group(1) if match else text
