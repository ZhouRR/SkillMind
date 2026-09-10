"""二進の設計書を platform 側で text 化する単一実装 (計画 §19 W5 / §20 R4)。

Agent には text だけを見せる。原本を生のまま物化しても `workspace.read/search` は UTF-8 しか
扱えず「読めない」で終わるため、**位置情報を保ったまま**平文へ落とす。位置を残すのは「設計書の
この sheet のこの cell」「この段落」という参照を Evidence として指せるようにするためで、単なる
本文抽出ではその追跡性が失われる。

対応形式は xlsx/xlsm (表) と docx (文書)。いずれも OPC (ZIP + XML) であり、標準 library だけで
必要な範囲を読めるため**外部依存を増やさない**。PDF は構造上 parser 実装か新依存が必要で、
費用対効果が変わるため対象外のままとする (読めない binary は従来どおり `skipped` に残る)。
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from io import BytesIO
from xml.etree import ElementTree

# 変換対象の拡張子。xlsm は容器構造が xlsx と同一のため同じ経路で読める (macro は解釈しない)。
_SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm")
_DOCUMENT_SUFFIXES = (".docx",)
SUPPORTED_SUFFIXES = _SPREADSHEET_SUFFIXES + _DOCUMENT_SUFFIXES

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PACKAGE_RELS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_RELS_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# ZIP 爆弾対策。展開後の総 byte と entry 数を宣言値の段階で拒否する (実際に展開しない)。
_MAX_UNCOMPRESSED_BYTES = 33_554_432
_MAX_ZIP_ENTRIES = 512
# 1 entry あたりの展開上限。sharedStrings や大 sheet の XML はこの範囲に収まる想定。
_MAX_ENTRY_BYTES = 16_777_216


class BinaryTextError(RuntimeError):
    """対応形式として読めない、または安全に展開できないことを表す。"""


# 旧名。既存の呼び出し・テストを壊さずに済ませるための別名 (意味は同一)。
SpreadsheetTextError = BinaryTextError


def is_textualizable(path: str) -> bool:
    """Path が text 化対象の拡張子かを判定する。"""

    lowered = path.lower()
    return lowered.endswith(SUPPORTED_SUFFIXES)


def render_text(path: str, data: bytes) -> str:
    """拡張子に応じて text 化する。対応外は呼び出し前に `is_textualizable` で弾く。"""

    lowered = path.lower()
    if lowered.endswith(_SPREADSHEET_SUFFIXES):
        return render_spreadsheet_text(data)
    if lowered.endswith(_DOCUMENT_SUFFIXES):
        return render_document_text(data)
    raise BinaryTextError("Unsupported binary format")


def render_document_text(data: bytes) -> str:
    """docx の本文を「段落番号付きの行」と「表の行」へ落とす。

    段落番号 (`P12:`) と表座標 (`T2R3:`) を前置するのは xlsx の cell 座標と同じ理由——検索が
    当たった行から、その内容が文書のどこに在るかを Evidence として指せるようにするため。
    見出し段落は `#` を付けて構造を残す。空段落は出力しない。
    """

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            _ensure_safe_archive(archive)
            body = _read_part(archive, "word/document.xml")
            if body is None:
                raise BinaryTextError("Document has no body part")
            lines = list(_read_document_blocks(body))
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError) as error:
        raise BinaryTextError("Document could not be read") from error
    if not lines:
        raise BinaryTextError("Document contains no readable text")
    return "\n".join(lines) + "\n"


def _read_document_blocks(body: ElementTree.Element) -> Iterator[str]:
    """本文を文書順に走査し、段落と表を行 text へ変換する。"""

    found = body.find(f"{_WORD_NS}body")
    root = body if found is None else found
    paragraph_index = 0
    table_index = 0
    for block in root:
        if block.tag == f"{_WORD_NS}p":
            paragraph_index += 1
            text = _paragraph_text(block)
            if text:
                yield f"P{paragraph_index}: {_heading_prefix(block)}{text}"
        elif block.tag == f"{_WORD_NS}tbl":
            table_index += 1
            for row_index, row in enumerate(block.findall(f"{_WORD_NS}tr"), start=1):
                cells = [
                    _collapse("".join(node.text or "" for node in cell.iter(f"{_WORD_NS}t")))
                    for cell in row.findall(f"{_WORD_NS}tc")
                ]
                if any(cells):
                    yield f"T{table_index}R{row_index}: " + " | ".join(cells)


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    """段落配下の文字 run を連結する (field code や図は取らない)。"""

    return _collapse("".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")))


def _heading_prefix(paragraph: ElementTree.Element) -> str:
    """見出し段落に markdown 風の `#` を付け、文書構造を text に残す。"""

    properties = paragraph.find(f"{_WORD_NS}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{_WORD_NS}pStyle")
    value = style.get(f"{_WORD_NS}val") if style is not None else None
    if not isinstance(value, str) or not value.lower().startswith("heading"):
        return ""
    level = value[len("heading") :].strip()
    depth = int(level) if level.isdigit() and 1 <= int(level) <= 6 else 1
    return "#" * depth + " "


def render_spreadsheet_text(data: bytes) -> str:
    """xlsx の全 sheet を「sheet 見出し + 行ごとの `座標: 値`」形式の text へ落とす。

    行単位で束ねるのは、検索が当たった行から同じ行の他 cell (対応する担当者名など) を一度に
    読めるようにするため。cell 座標を各値に付けるのは、その値が sheet のどこに在るかを
    Evidence として指せるようにするため。空 cell は出力しない。

    数式 cell は保存済みの計算結果 (`<v>`) を採る。日時は xlsx の格納形式である serial 値の
    まま出す (書式解釈は行わない)。表示形式の再現は目的ではなく、内容の可読化と可指定化が目的。
    """

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            _ensure_safe_archive(archive)
            shared_strings = _read_shared_strings(archive)
            sheets = _read_sheet_targets(archive)
            sections: list[str] = []
            for name, target in sheets:
                rows = list(_read_sheet_rows(archive, target, shared_strings))
                body = "\n".join(rows)
                sections.append(f"## Sheet: {name}\n{body}" if body else f"## Sheet: {name}")
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError) as error:
        raise BinaryTextError("Spreadsheet could not be read") from error
    if not sections:
        raise BinaryTextError("Spreadsheet contains no readable sheet")
    return "\n\n".join(sections) + "\n"


def _ensure_safe_archive(archive: zipfile.ZipFile) -> None:
    """展開前に宣言 size と entry 数で ZIP 爆弾を弾く。"""

    entries = archive.infolist()
    if len(entries) > _MAX_ZIP_ENTRIES:
        raise BinaryTextError("Spreadsheet contains too many parts")
    total = 0
    for entry in entries:
        if entry.file_size > _MAX_ENTRY_BYTES:
            raise BinaryTextError("Spreadsheet part exceeds the expansion limit")
        total += entry.file_size
    if total > _MAX_UNCOMPRESSED_BYTES:
        raise BinaryTextError("Spreadsheet exceeds the expansion limit")


def _read_part(archive: zipfile.ZipFile, name: str) -> ElementTree.Element | None:
    """Archive 内の XML part を読む。存在しない part は None を返す。"""

    try:
        raw = archive.read(name)
    except KeyError:
        return None
    return ElementTree.fromstring(raw.decode("utf-8", errors="replace"))


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """共有文字列表を index 順で読む。rich text は run を連結する。"""

    root = _read_part(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    values: list[str] = []
    for item in root.findall(f"{_MAIN_NS}si"):
        # rich text は <r><t> の並びになるため、si 配下の全 <t> を順に連結する。
        values.append("".join(node.text or "" for node in item.iter(f"{_MAIN_NS}t")))
    return values


def _read_sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Workbook の sheet 名と worksheet part path を宣言順で解決する。"""

    workbook = _read_part(archive, "xl/workbook.xml")
    relationships = _read_part(archive, "xl/_rels/workbook.xml.rels")
    if workbook is None:
        raise BinaryTextError("Spreadsheet has no workbook part")
    targets: dict[str, str] = {}
    if relationships is not None:
        for relationship in relationships.findall(f"{_PACKAGE_RELS_NS}Relationship"):
            identifier = relationship.get("Id")
            target = relationship.get("Target")
            if identifier and target:
                targets[identifier] = _normalize_part(target)
    sheets: list[tuple[str, str]] = []
    for index, sheet in enumerate(workbook.iter(f"{_MAIN_NS}sheet"), start=1):
        name = sheet.get("name") or f"Sheet{index}"
        identifier = sheet.get(f"{_OFFICE_RELS_NS}id")
        part = targets.get(identifier or "", f"xl/worksheets/sheet{index}.xml")
        sheets.append((name, part))
    return sheets


def _normalize_part(target: str) -> str:
    """Relationship の Target を archive 内 path へ正規化する。"""

    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"


def _read_sheet_rows(
    archive: zipfile.ZipFile, part: str, shared_strings: list[str]
) -> Iterator[str]:
    """一 sheet を「`A1: 値 | B1: 値`」形式の行 text へ変換する。"""

    root = _read_part(archive, part)
    if root is None:
        return
    for row in root.iter(f"{_MAIN_NS}row"):
        cells = [
            f"{reference}: {value}"
            for reference, value in (
                (cell.get("r") or "", _cell_text(cell, shared_strings))
                for cell in row.findall(f"{_MAIN_NS}c")
            )
            if value
        ]
        if cells:
            yield " | ".join(cells)


def _cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    """Cell の格納値を型別に平文へ落とす。空 cell は空文字を返す。"""

    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(f"{_MAIN_NS}is")
        if inline is None:
            return ""
        return _collapse("".join(node.text or "" for node in inline.iter(f"{_MAIN_NS}t")))
    value = cell.findtext(f"{_MAIN_NS}v")
    if value is None:
        return ""
    if kind == "s":
        # 共有文字列 index。範囲外は壊れた file なので空にせず、追跡できるよう素値を残す。
        try:
            return _collapse(shared_strings[int(value)])
        except (ValueError, IndexError):
            return _collapse(value)
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    return _collapse(value)


def _collapse(value: str) -> str:
    """Cell 内改行と tab を 1 行へ畳む (行単位検索の粒度を壊さないため)。"""

    return " ".join(value.split())
