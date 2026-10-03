"""部分表の表示に原文の表頭と完全な行だけを使う。"""

from __future__ import annotations

import pytest

from skillmind.core.markdown_excerpt import markdown_table_preview

TABLE = (
    "# Scope\n\n| Case | Expected |\n| :--- | ---: |\n"
    "| A | first |\n| B | second |\n| C | third |\n"
)


def test_middle_row_preview_keeps_original_header_and_complete_rows() -> None:
    """途中の先頭・末尾行は補完せず、完全な行だけを表頭へ関連づける。"""

    preview = markdown_table_preview(TABLE, TABLE.index("first") + 1, TABLE.index("third") + 2)
    assert preview == {
        "version": "markdown-table/v1",
        "source": "| Case | Expected |\n| :--- | ---: |\n| B | second |\n",
        "header_line_start": 3,
        "line_start": 6,
        "line_end": 6,
    }


@pytest.mark.parametrize(
    "source",
    [
        "text | prose\n| x | y |\n| A | first |\n",
        "```text\n" + TABLE + "```\n",
        "````text\n```\n" + TABLE + "````\n",
        "\n".join("    " + line for line in TABLE.splitlines()),
    ],
)
def test_no_header_is_invented_for_prose_or_code(source: str) -> None:
    """通常本文と code block は原文表示へ戻す。"""

    assert markdown_table_preview(source, source.index("first"), len(source)) is None


def test_multiple_tables_use_the_containing_table_not_the_first() -> None:
    """前の表の列を借用せず、実際に対象行を含む表だけを使う。"""

    source = TABLE + "\n| Task | Status |\n| --- | --- |\n| X | Ready |\n| Y | Done |\n"
    preview = markdown_table_preview(source, source.index("Ready") + 2, len(source))
    assert preview is not None
    assert preview["source"] == "| Task | Status |\n| --- | --- |\n| Y | Done |\n"


def test_complete_table_and_non_table_tail_keep_normal_rendering() -> None:
    """表以外の内容を部分表で置き換えない。"""

    assert markdown_table_preview(TABLE, 0, len(TABLE)) is None
    tail = TABLE + "\nOriginal conclusion.\n"
    assert markdown_table_preview(tail, tail.index("first"), len(tail)) is None


def test_unconfirmed_eof_does_not_promote_a_partial_row() -> None:
    """保存済み応答の末尾を原 file の終端と扱わない。"""

    source = TABLE.rstrip("\n")
    assert (
        markdown_table_preview(source, source.index("second"), len(source), complete=False) is None
    )


def test_oversized_header_does_not_expand_the_preview_budget() -> None:
    """表示用 metadata の予算を超えた場合は原文を保持する。"""

    source = "| " + "large" * 1000 + " | Expected |\n| --- | --- |\n| A | one |\n| B | two |\n"
    assert markdown_table_preview(source, source.index("one"), len(source)) is None


@pytest.mark.parametrize("header", ["| Case |\n| --- |\n", "| A\\|B | Expected |\n| --- | --- |\n"])
def test_single_column_and_escaped_header_keep_the_original_columns(header: str) -> None:
    """単列と label 内 pipe も原文の列数どおり扱う。"""

    source = header + "| A | first |\n| B | second |\n"
    preview = markdown_table_preview(source, source.index("first"), len(source))
    assert preview is not None and preview["source"].startswith(header)


def test_inconsistent_header_and_separator_do_not_create_a_table() -> None:
    """元の Markdown で成立しない列数を補正して表を作らない。"""

    source = "| A | B | C |\n| --- | --- |\n| X | first |\n| Y | second |\n"
    assert markdown_table_preview(source, source.index("first"), len(source)) is None
