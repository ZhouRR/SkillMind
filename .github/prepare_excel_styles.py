"""検証 branch で固定した既存入口に新 converter を接続する一回限りの準備。"""
from pathlib import Path
import hashlib


def edit(path: str, expected: str, changes: list[tuple[str, str]]) -> None:
    """元 blob と置換位置を確認し、他の利用者変更を上書きしない。"""
    p = Path(path)
    raw = p.read_bytes()
    assert hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == expected, path
    text = raw.decode('utf-8')
    for old, new in changes:
        assert text.count(old) == 1, (path, old)
        text = text.replace(old, new)
    p.write_text(text, encoding='utf-8')


edit('SKM/backend/src/skillmind/agent/binary_text.py', '51557a0c6cd0f28c13899f6c2e210343384e73b9', [
    ('MARKITDOWN_VERSION = "0.1.7"', 'MARKITDOWN_VERSION = "0.1.7"\nEXCEL_STYLE_PROFILE = "excel-styles/v1"\nEXCEL_VALUE_PROFILE = "excel-values/v1"'),
    ('    converter_version: str = MARKITDOWN_VERSION', '    converter_version: str = MARKITDOWN_VERSION\n    profile: str = EXCEL_STYLE_PROFILE'),
    ('    return MarkdownConversion(markdown)', '    return MarkdownConversion(\n        markdown, profile=EXCEL_STYLE_PROFILE if suffix == ".xlsx" else EXCEL_VALUE_PROFILE\n    )'),
    ('    sheets = pd.read_excel(\n        BytesIO(data), sheet_name=None,', '    sheets = pd.read_excel(\n        BytesIO(data), sheet_name=None, header=None if suffix == ".xlsx" else 0,'),
    ('''    converter = HtmlConverter()
    sections = [
        f"## {name}\\n{converter.convert_string(sheet.to_html(index=False)).markdown.strip()}"
        for name, sheet in sheets.items()
    ]
    markdown = "\\n\\n".join(sections).strip()''', '''    converter = HtmlConverter()
    if suffix == ".xlsx":
        from skillmind.agent.excel_markdown import render_styled_sheets

        markdown = render_styled_sheets(
            data, sheets, lambda html: converter.convert_string(html, strict=True).markdown.strip()
        )
    else:
        sections = [
            f"## {name}\\n{converter.convert_string(sheet.to_html(index=False)).markdown.strip()}"
            for name, sheet in sheets.items()
        ]
        markdown = "\\n\\n".join(sections).strip()
        markdown += (
            "\\n\\n> Excel format coverage: excel-values/v1. Legacy XLS cell values only; "
            "strikethrough, fills and conditional formatting were NOT inspected. "
            "Do not infer absence of formatting or exclude rows from colors alone."
        )'''),
])
edit('SKM/backend/src/skillmind/agent/document_provider.py', 'f8e6aa96098c0041c5e36f0908dd266a5a06774d', [
    ('''                    "Conversion may omit formatting, drawings, merged-cell structure and formulas. "
                    "Review conversion loss separately; do not infer original cell coordinates."''', '''                    "See the Markdown's format coverage and per-sheet style facts. Static XLSX "
                    "strikethrough and fills are not final conditional formatting or business "
                    "scope. Legacy XLS styles are not inspected. Drawings, calculated formatting "
                    "and formula evaluation are not preserved; original bytes remain authoritative."'''),
    ('**_source_metadata(content), "converter": converter,\n                    "markdown_checksum": markdown_hash,', '**_source_metadata(content), "converter": converter,\n                    "conversion_profile": converted.profile,\n                    "markdown_checksum": markdown_hash,'),
])
p = Path('docs/development/runtime-guide.md')
text = p.read_text(encoding='utf-8')
anchor = '\n## 执行与恢复\n'
assert text.count(anchor) == 1
text = text.replace(anchor, '\nExcel→Markdown 的 `.xlsx` 路径使用 `excel-styles/v1`：同一文档保留原行列、静态整格/局部删除线和任意填充色，重复样式按实际连续范围合并。色值保留原 RGB/theme/indexed/tint 表示；无法解色不假定白色。条件格式与表格样式只标记范围和未求值状态，不据此自动排除业务步骤。合并区域记录 anchor 与范围，不展开复制正文。旧 `.xls` 仍为值转换，并在保存的 Markdown 明示未检查样式。转换 profile 写入 Evidence，原文件/冻结版本、Artifact 原字节校验及既有限制不变。\n' + anchor)
p.write_text(text, encoding='utf-8')

edit('SKM/backend/src/skillmind/agent/excel_markdown.py', '9b2edbb9ec15ee0024e512fd2493cfc7969038c5', [
    ('    rich_strikes: list[dict[str, Any]] = []\n    for (row, column)',
     '    rich_strikes: list[dict[str, Any]] = []\n    styles: dict[int, dict[str, Any]] = {}\n    for (row, column)'),
    ('        detail: dict[str, Any] = {"fill": colors.fill(cell.fill)}\n        if cell.font.strike is not None:\n            detail["base_font_strike"] = cell.font.strike', '        # 同じ workbook の同じ immutable style は一度だけ解色する。\n        detail = styles.get(cell.style_id)\n        if detail is None:\n            detail = {"fill": colors.fill(cell.fill)}\n            if cell.font.strike is not None:\n                detail["base_font_strike"] = cell.font.strike\n            styles[cell.style_id] = detail'),
    ('the final displayed color. Colors/strikethrough do not decide business scope.',
     'the final displayed color. The converter infers no exclusions; interpret style facts using the source legend and business rules.'),
    ('            facts = _style_facts(sheet, colors) if',
     '            facts: dict[str, Any] = _style_facts(sheet, colors) if'),
])

edit('SKM/backend/tests/agent/test_excel_markdown.py', '335747b2f7a7088044f2643f773b49e7ee414ed4', [
    ('from openpyxl import Workbook\n', 'from openpyxl import Workbook, load_workbook\n'),
    ('    sheet["B3"] = "SKMCELLTOKEN0END"\n    markdown = _convert(book)\n', '    sheet["B3"] = "SKMCELLTOKEN0END"\n    # stdlib XML writer は CR を生で出力し、再読時に LF へ正規化される。\n    # fixture 自体が原 CR を保存するよう文字参照にし、converter の断言は弱めない。\n    raw = _bytes(book)\n    target = BytesIO()\n    with ZipFile(BytesIO(raw)) as source, ZipFile(target, "w", ZIP_DEFLATED) as archive:\n        for item in source.infolist():\n            data = source.read(item.filename)\n            if item.filename == "xl/worksheets/sheet1.xml":\n                data = data.replace(b"\\r", b"&#13;")\n            archive.writestr(item.filename, data)\n    prepared = target.getvalue()\n    verified = load_workbook(BytesIO(prepared), data_only=True, rich_text=True)\n    try:\n        assert verified["Cases"]["B2"].value == "  001|<script>\\r\\nNA\\t~~old~~  "\n    finally:\n        verified.close()\n    markdown = render_excel_markdown(".xlsx", prepared)\n'),
])

p = Path('SKM/backend/tests/agent/test_excel_markdown.py')
p.write_text(p.read_text(encoding='utf-8') + '\n\ndef test_repeated_cell_style_is_resolved_once(monkeypatch: pytest.MonkeyPatch) -> None:\n    """同色の長い表でセル数に比例した fill 複製/解色を繰り返さない。"""\n    book = _book()\n    for row in range(2, 502):\n        book["Cases"].cell(row, 2, "item").fill = PatternFill("solid", fgColor="FFABCDEF")\n    calls = 0\n    original = _Colors.fill\n\n    def counted(self: _Colors, fill: Any) -> dict[str, Any]:\n        """解色の実呼出しを計測し、処理内容は変えない。"""\n        nonlocal calls\n        calls += 1\n        return original(self, fill)\n\n    monkeypatch.setattr(_Colors, "fill", counted)\n    markdown = _convert(book)\n    assert calls == 2\n    assert _facts(markdown)["cell_styles"][0]["ranges"] == ["B2:B501"]\n', encoding='utf-8')
