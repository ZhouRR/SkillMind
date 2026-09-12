"""実 MarkItDown と凍結文書 Provider の変換・権限・停止境界を検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openpyxl import Workbook

from skillmind.agent import binary_text
from skillmind.agent.binary_text import BinaryTextError, convert_excel_to_markdown
from skillmind.agent.context_builder import ContractStore
from skillmind.agent.document_provider import DocumentConvertProvider
from skillmind.agent.tool_gateway import RunToolContext, ToolProviderError
from skillmind.core.hashing import sha256_hex
from skillmind.documents.source import ProjectDocumentContent
from skillmind.storage.observation import BlobObservation
from tests.agent.test_document_provider import _context, _FakeSource
from tests.documents.fakes import document_content

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def excel_bytes() -> bytes:
    """複数 sheet と日本語の観測結果を含む正規 XLSX をメモリ内で作る。"""

    book = Workbook()
    book.remove(book.active)
    for name in ("Cases", "Boundaries"):
        sheet = book.create_sheet(name)
        sheet.append(["ID", "Expected"])
        sheet.append(["TC-1", "正常終了"])
        sheet.append(["TC-2", "入力エラー"])
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def _conversion_context(content: ProjectDocumentContent) -> RunToolContext:
    """変換だけを許可した Run を作り、read 権の自動拡張に依存しない。"""

    context = _context(uuid4(), content=content)
    assert context.run is not None
    tool = replace(context.tool, capability="document.convert/v1")
    sources = {key: {**value, "capability": "document.convert/v1"}
               for key, value in context.run.resolved_sources.items()}
    run = replace(context.run, tools=(tool,), resolved_sources=sources,
                  permission_snapshot={"allowed_capabilities": ["document.convert/v1"]})
    return replace(context, tool=tool, run=run)


@pytest.mark.parametrize("suffix", [".xlsx", ".xls"])
async def test_real_converter_preserves_sheets_and_japanese_rows(
    excel_bytes: bytes, suffix: str,
) -> None:
    """固定 converter を実 process で呼び、二つの Excel 形式を証明する。"""

    data = excel_bytes if suffix == ".xlsx" else (
        ROOT / "backend/tests/fixtures/documents/conversion-sample.xls"
    ).read_bytes()
    result = await convert_excel_to_markdown("sample" + suffix, data)
    assert result.converter_version == "0.1.7"
    assert "## Cases" in result.markdown and "## Boundaries" in result.markdown
    assert "| TC-1 | 正常終了 |" in result.markdown
    assert "| TC-2 | 入力エラー |" in result.markdown


@pytest.mark.parametrize("observed", [False, True])
async def test_provider_returns_complete_markdown_bound_to_original_hash(
    excel_bytes: bytes, observed: bool,
) -> None:
    """公開契約・Evidence と原文/変換文 hash が同じ取得 bytes に結び付く。"""

    content = document_content(excel_bytes, name="sample.xlsx")
    if observed:
        content = replace(content, observation=BlobObservation(
            last_modified=datetime(2026, 9, 11, tzinfo=UTC), etag="opaque",
            version_id="original-version", size=len(excel_bytes), content_type=content.mime,
        ))
    context = _conversion_context(content)
    source = _FakeSource(project_id=context.project_id, content=content)
    result = await DocumentConvertProvider(source).execute(context, {"path": "specs/sample.xlsx"})
    response = dict(result.response)
    response["evidence_refs"] = ["ev_markdown_001"]
    schema = ContractStore(ROOT / "contracts").load(
        "tools/document.convert/v1/response.schema.json"
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response)
    assert response["document"]["checksum"] == content.checksum
    assert response["markdown_checksum"] == "sha256:" + sha256_hex(response["markdown"].encode())
    assert result.evidence[0].content_hash == content.checksum
    assert result.evidence[0].metadata["markdown_checksum"] == response["markdown_checksum"]
    assert result.evidence[0].metadata["converter"] == response["converter"]
    if content.observation is not None:
        assert result.evidence[0].metadata["storage_observation"] == content.observation.to_json()
    else:
        assert "storage_observation" not in result.evidence[0].metadata
    assert response["warnings"]
    assert "line_start" not in result.evidence[0].source_locator
    assert source.calls == [content.document_id]


async def test_read_permission_does_not_authorize_conversion(excel_bytes: bytes) -> None:
    """旧 Run の read 権では blob 読取・変換とも開始しない。"""

    content = document_content(excel_bytes, name="sample.xlsx")
    context = _context(uuid4(), content=content)
    source = _FakeSource(project_id=context.project_id, content=content)
    with pytest.raises(ToolProviderError, match="not allowed"):
        await DocumentConvertProvider(source).execute(context, {"path": "specs/sample.xlsx"})
    assert source.calls == []


@pytest.mark.parametrize("path", ["../sample.xlsx", "specs/unselected.xlsx", "/sample.xlsx"])
async def test_unselected_or_unsafe_path_never_reads_storage(excel_bytes: bytes, path: str) -> None:
    """凍結範囲外 path を存在確認のためにも fetch しない。"""

    content = document_content(excel_bytes, name="sample.xlsx")
    context = _conversion_context(content)
    source = _FakeSource(project_id=context.project_id, content=content)
    with pytest.raises(ToolProviderError):
        await DocumentConvertProvider(source).execute(context, {"path": path})
    assert source.calls == []


async def test_replaced_content_fails_before_conversion(
    excel_bytes: bytes,
) -> None:
    """同じ ID の byte 差替えを元 checksum の不一致として拒否する。"""

    content = document_content(excel_bytes, name="sample.xlsx")
    context = _conversion_context(content)
    source = _FakeSource(project_id=context.project_id, content=replace(content, data=b"changed"))
    with pytest.raises(ToolProviderError) as caught:
        await DocumentConvertProvider(source).execute(context, {"path": "specs/sample.xlsx"})
    assert caught.value.code == "unavailable"


@pytest.mark.parametrize("name,data", [
    ("sample.xlsm", b"ignored"), ("sample.xlsx", b"broken"), ("sample.xls", b"broken"),
])
async def test_unsupported_and_corrupt_workbooks_fail(name: str, data: bytes) -> None:
    """非対象形式や偽装 Excel を変換成功へ丸めない。"""

    with pytest.raises(BinaryTextError):
        await convert_excel_to_markdown(name, data)


async def test_zip_expansion_limit_rejects_before_process() -> None:
    """小さい圧縮入力でも宣言展開量が上限を超えるなら拒否する。"""

    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", b"x" * (16_777_216 + 1))
    with pytest.raises(BinaryTextError, match="expansion limit"):
        await convert_excel_to_markdown("sample.xlsx", stream.getvalue())


async def test_output_limit_does_not_return_truncated_success(
    excel_bytes: bytes, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """親 process 側の出力上限も独立に適用し、成功として切り詰めない。"""

    monkeypatch.setattr(binary_text, "MAX_MARKDOWN_BYTES", 10)
    with pytest.raises(BinaryTextError, match="output limit"):
        await convert_excel_to_markdown("sample.xlsx", excel_bytes)


@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_and_timeout_kill_and_reap_conversion_process(
    excel_bytes: bytes, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    """実 child が入力待ちの時点で中断し、例外返却前に停止済みを確認する。"""

    started = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def hold(process: asyncio.subprocess.Process, data: bytes) -> str:
        """子へ本文を渡す前の境界で待機する。"""

        processes.append(process)
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr(binary_text, "_exchange_markdown", hold)
    if not cancel:
        monkeypatch.setattr(binary_text, "_CONVERSION_TIMEOUT_SECONDS", 0.1)
    task = asyncio.create_task(convert_excel_to_markdown("sample.xlsx", excel_bytes))
    await asyncio.wait_for(started.wait(), timeout=5)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else BinaryTextError):
        await task
    assert len(processes) == 1 and processes[0].returncode is not None
