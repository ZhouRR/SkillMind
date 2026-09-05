"""二進設計書 (xlsx / docx) の platform 側 text 化を検証する (計画 §19 W5 / §20 R4)。

外部 library を使わずに読むため、test 側でも標準 library だけで最小の OPC package を組み立て、
実際の構造 (共有文字列・inline 文字列・数式の保存値・複数 sheet・段落・表・見出し) に対して
検証する。
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from projectmind.agent.binary_text import (
    BinaryTextError,
    is_textualizable,
    render_document_text,
    render_spreadsheet_text,
    render_text,
)

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _workbook(sheets: list[tuple[str, str]]) -> bytes:
    """sheet 名と worksheet XML から最小の xlsx を組み立てる。"""

    entries: dict[str, str] = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_OFFICE_RELS}"><sheets>'
            + "".join(
                f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
                for index, (name, _) in enumerate(sheets, start=1)
            )
            + "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{_PACKAGE_RELS}">'
            + "".join(
                f'<Relationship Id="rId{index}" Type="{_OFFICE_RELS}/worksheet" '
                f'Target="worksheets/sheet{index}.xml"/>'
                for index in range(1, len(sheets) + 1)
            )
            + "</Relationships>"
        ),
    }
    for index, (_, body) in enumerate(sheets, start=1):
        entries[f"xl/worksheets/sheet{index}.xml"] = (
            f'<worksheet xmlns="{_MAIN}"><sheetData>{body}</sheetData></worksheet>'
        )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _with_shared_strings(data: bytes, values: list[str]) -> bytes:
    """既存 archive へ共有文字列表を足す。"""

    shared = (
        f'<sst xmlns="{_MAIN}">'
        + "".join(f"<si><t>{value}</t></si>" for value in values)
        + "</sst>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w") as target:
        for item in source.infolist():
            target.writestr(item.filename, source.read(item.filename))
        target.writestr("xl/sharedStrings.xml", shared)
    return buffer.getvalue()


def test_renders_sheet_name_and_cell_coordinates() -> None:
    """sheet 名を見出しに、各値へ cell 座標を付けて行単位で出力する。"""

    body = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>42</v></c></row>'
    )
    data = _with_shared_strings(
        _workbook([("規模一覧", body)]), ["機能ID", "担当者", "F-001"]
    )

    text = render_spreadsheet_text(data)

    assert text.splitlines() == [
        "## Sheet: 規模一覧",
        "A1: 機能ID | B1: 担当者",
        "A2: F-001 | B2: 42",
    ]


def test_inline_strings_booleans_and_empty_cells() -> None:
    """inline 文字列と真偽値を平文化し、空 cell は出力しない。"""

    body = (
        '<row r="1"><c r="A1" t="inlineStr"><is><t>設計</t></is></c>'
        '<c r="B1"/><c r="C1" t="b"><v>1</v></c></row>'
    )

    text = render_spreadsheet_text(_workbook([("Sheet1", body)]))

    assert text.splitlines() == ["## Sheet: Sheet1", "A1: 設計 | C1: TRUE"]


def test_multiple_sheets_keep_declaration_order() -> None:
    """複数 sheet は宣言順に、それぞれ見出し付きで出力する。"""

    first = '<row r="1"><c r="A1" t="inlineStr"><is><t>one</t></is></c></row>'
    second = '<row r="1"><c r="A1" t="inlineStr"><is><t>two</t></is></c></row>'

    text = render_spreadsheet_text(_workbook([("First", first), ("Second", second)]))

    assert text.splitlines() == [
        "## Sheet: First",
        "A1: one",
        "",
        "## Sheet: Second",
        "A1: two",
    ]


def test_cell_newlines_are_collapsed_to_keep_line_granularity() -> None:
    """cell 内改行は 1 行へ畳む (行単位検索の粒度と座標対応を壊さない)。"""

    body = '<row r="1"><c r="A1" t="inlineStr"><is><t>上段\n下段</t></is></c></row>'

    text = render_spreadsheet_text(_workbook([("Sheet1", body)]))

    assert "A1: 上段 下段" in text


def test_non_spreadsheet_bytes_fail_closed() -> None:
    """xlsx でない byte 列は変換成功として扱わない。"""

    with pytest.raises(BinaryTextError):
        render_spreadsheet_text(b"\xff\xfe\x00not a zip")


def test_declared_expansion_limit_is_rejected_before_reading() -> None:
    """展開後 size の宣言値が上限を超える archive は展開前に拒否する (ZIP 爆弾対策)。"""

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", "0" * 20_000_000)

    with pytest.raises(BinaryTextError):
        render_spreadsheet_text(buffer.getvalue())


def test_supported_suffix_detection() -> None:
    """xlsx/xlsm/docx を変換対象とみなし、PDF は対象外のままとする。"""

    assert is_textualizable("input/documents/spec.xlsx") is True
    assert is_textualizable("input/documents/MACRO.XLSM") is True
    assert is_textualizable("input/documents/spec.docx") is True
    assert is_textualizable("input/documents/spec.pdf") is False


_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _document(body: str) -> bytes:
    """最小の docx を組み立てる (`word/document.xml` だけを持つ package)。"""

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<document xmlns="{_WORD}"><body>{body}</body></document>',
        )
    return buffer.getvalue()


def _paragraph(text: str, *, style: str | None = None) -> str:
    """段落 XML を作る。style を渡すと見出し段落になる。"""

    properties = (
        f'<pPr><pStyle xmlns:w="{_WORD}" w:val="{style}"/></pPr>' if style else ""
    )
    return f"<p>{properties}<r><t>{text}</t></r></p>"


def test_document_paragraphs_keep_their_position() -> None:
    """段落は本文順の番号付きで出力し、空段落は落とす。"""

    body = _paragraph("認証境界の設計") + "<p><r><t></t></r></p>" + _paragraph("例外は無い")

    text = render_document_text(_document(body))

    assert text.splitlines() == ["P1: 認証境界の設計", "P3: 例外は無い"]


def test_document_headings_carry_their_level() -> None:
    """見出し段落は level 付きで構造を残す。"""

    body = _paragraph("第 1 章", style="Heading1") + _paragraph("詳細", style="Heading2")

    text = render_document_text(_document(body))

    assert text.splitlines() == ["P1: # 第 1 章", "P2: ## 詳細"]


def test_document_tables_are_rendered_by_row_with_coordinates() -> None:
    """表は行単位に座標付きで出力し、同じ行の他列を一度に読めるようにする。"""

    body = (
        "<tbl>"
        "<tr><tc><p><r><t>機能ID</t></r></p></tc><tc><p><r><t>担当者</t></r></p></tc></tr>"
        "<tr><tc><p><r><t>F-001</t></r></p></tc><tc><p><r><t>山田</t></r></p></tc></tr>"
        "</tbl>"
    )

    text = render_document_text(_document(body))

    assert text.splitlines() == [
        "T1R1: 機能ID | 担当者",
        "T1R2: F-001 | 山田",
    ]


def test_document_and_spreadsheet_share_one_entry_point() -> None:
    """拡張子による振り分けは単一の入口が行う (物化経路は形式を意識しない)。"""

    workbook = _with_shared_strings(
        _workbook([("一覧", '<row r="1"><c r="A1" t="s"><v>0</v></c></row>')]), ["値"]
    )

    assert "## Sheet: 一覧" in render_text("input/documents/a.xlsx", workbook)
    assert "P1: 本文" in render_text("input/documents/a.docx", _document(_paragraph("本文")))
    with pytest.raises(BinaryTextError):
        render_text("input/documents/a.pdf", b"%PDF-1.7")


def test_broken_document_fails_closed() -> None:
    """docx として読めない byte 列は変換成功として扱わない。"""

    with pytest.raises(BinaryTextError):
        render_document_text(b"\xff\xfe\x00not a package")
