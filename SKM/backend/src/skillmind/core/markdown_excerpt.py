"""原文の表頭と完全な行だけを使い、部分抜粋の表示用 Markdown を補う。"""

from __future__ import annotations

import re
from typing import Any

MAX_PREVIEW_CHARACTERS = 4_096
MAX_CONTEXT_CHARACTERS = 1_048_576


def markdown_table_preview(
    text: str,
    start: int,
    end: int,
    *,
    complete: bool = True,
) -> dict[str, Any] | None:
    """途中の表を原文で確認できる場合だけ表頭を再掲し、切れた行は補造しない。"""

    if not 0 <= start < end <= len(text) or start > MAX_CONTEXT_CHARACTERS:
        return None
    # 原 file が大きくても、対象抜粋までの有界 prefix だけを調べる。
    prefix = text[:end]
    lines = prefix.splitlines(keepends=True)
    position = 0
    fence: tuple[str, int] | None = None
    header: tuple[int, str] | None = None
    table_start = -1
    body: list[str] = []
    first_line = last_line = 0
    for index, line in enumerate(lines):
        line_start, position = position, position + len(line)
        finished = line.endswith(("\n", "\r")) or (complete and end == len(text))
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            symbol = marker[1][0]
            if fence is None:
                fence = (symbol, len(marker[1]))
            elif (
                symbol == fence[0]
                and len(marker[1]) >= fence[1]
                and not line[marker.end() :].strip()
            ):
                fence = None
            header = None
            body.clear()
            continue
        if fence is not None or line.startswith(("    ", "\t")):
            header = None
            body.clear()
            continue
        columns = _separator_columns(line)
        if (
            index
            and columns
            and "|" in lines[index - 1]
            and not lines[index - 1].startswith(("    ", "\t"))
            and len(_pipe_cells(lines[index - 1])) == columns
        ):
            header = (index, lines[index - 1] + line)
            table_start = position
            body.clear()
            continue
        if header is not None and "|" in line and line.strip():
            # 表頭が元 window にある場合は通常の renderer に任せる。
            if table_start <= start and line_start >= start and finished:
                if not body:
                    first_line = index + 1
                body.append(line)
                last_line = index + 1
            continue
        # 抜粋が表以外へ続く場合は部分表を全抜粋の代わりにしない。
        header = None
        body.clear()
    if header is None or not body or table_start > start:
        return None
    source = header[1] + "".join(body)
    if len(source) > MAX_PREVIEW_CHARACTERS:
        return None
    return {
        "version": "markdown-table/v1",
        "source": source,
        "header_line_start": header[0],
        "line_start": first_line,
        "line_end": last_line,
    }


def _separator_columns(line: str) -> int:
    """GFM の表区切り行だけを認め、通常の pipe 本文から表頭を推測しない。"""

    if "|" not in line:
        return 0
    cells = _pipe_cells(line)
    return len(cells) if cells and all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells) else 0


def _pipe_cells(line: str) -> list[str]:
    """escape 済み pipe を列境界にせず、外側の任意 pipe だけを除く。"""

    row = line.strip()
    cells: list[str] = []
    start = 0
    escaped = False
    for index, character in enumerate(row):
        if character == "|" and not escaped:
            cells.append(row[start:index])
            start = index + 1
        escaped = not escaped if character == "\\" else False
    cells.append(row[start:])
    if cells and not cells[0] and row.startswith("|"):
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells
