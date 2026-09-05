"""Provider が共有する UTF-8 text の行範囲読み出しを単一実装に集約する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from projectmind.agent.tool_gateway import ToolProviderError

_MAX_CONTENT_CHARACTERS = 200_000


@dataclass(frozen=True, slots=True)
class TextLineWindow:
    """行範囲で切り出した内容と、応答へ載せる位置・切り詰め情報。"""

    content: str
    line_start: int
    line_end: int
    truncated: bool


def select_line_window(
    text: str,
    arguments: Mapping[str, Any],
    *,
    subject: str,
    allow_empty: bool,
) -> TextLineWindow:
    """引数の line_start/line_end を検証し、切り詰め済みの行範囲を返す。

    subject は公開 error message の主語 (例 "Repository", "Document")。allow_empty は
    空内容でも既定範囲を有効とするか (document.read/v1 は許可、Git fixture は従来どおり
    空 file を不正範囲として拒否する) を Provider ごとの既存契約のまま保持する。
    """

    lines = text.splitlines(keepends=True)
    default_end = max(1, len(lines)) if allow_empty else len(lines)
    line_start = _optional_positive_int(arguments.get("line_start"), default=1, subject=subject)
    line_end = _optional_positive_int(
        arguments.get("line_end"), default=default_end, subject=subject
    )
    if line_start > line_end or line_start > max(1, len(lines)):
        raise ToolProviderError(
            "invalid_request", f"{subject} line range is invalid", retryable=False
        )
    selected = "".join(lines[line_start - 1 : line_end])
    truncated = len(selected) > _MAX_CONTENT_CHARACTERS
    return TextLineWindow(
        content=selected[:_MAX_CONTENT_CHARACTERS],
        line_start=line_start,
        line_end=min(line_end, len(lines)),
        truncated=truncated,
    )


def _optional_positive_int(value: Any, *, default: int, subject: str) -> int:
    """Schema 検証後も bool を integer として扱わず、正数だけを返す。"""

    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolProviderError("invalid_request", f"{subject} line is invalid", retryable=False)
    return value
