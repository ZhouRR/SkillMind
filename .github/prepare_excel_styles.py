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
