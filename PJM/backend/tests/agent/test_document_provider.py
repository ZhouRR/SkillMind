"""DocumentProvider の project 作用域と document.read/v1 契約適合を検証する。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.agent.context_builder import ContractStore
from projectmind.agent.document_provider import DocumentProvider
from projectmind.agent.domain import RegisteredTool, RunWorkspace
from projectmind.agent.tool_gateway import RunToolContext, ToolProviderError
from projectmind.documents.source import ProjectDocumentContent

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
_CHECKSUM = "sha256:" + "a" * 64


class _FakeSource:
    """指定 project でのみ 1 文書を返す in-memory な ProjectDocumentSource。"""

    def __init__(self, *, project_id: UUID, content: ProjectDocumentContent) -> None:
        """所有 project と返す文書内容を保持する。"""

        self._project_id = project_id
        self._content = content

    async def fetch(
        self, *, project_id: UUID, folder: str, name: str
    ) -> ProjectDocumentContent | None:
        """所有 project かつ folder/name 一致時のみ内容を返す (越権は None)。"""

        if project_id != self._project_id:
            return None
        if folder == self._content.folder and name == self._content.name:
            return self._content
        return None


def _content(
    data: bytes = b"# Overview\nsecond line\n", *, mime: str = "text/markdown"
) -> ProjectDocumentContent:
    """検証用の文書内容を組み立てる。"""

    return ProjectDocumentContent(
        folder="specs", name="overview.md", mime=mime, checksum=_CHECKSUM, size=len(data), data=data
    )


def _context(project_id: UUID) -> RunToolContext:
    """document.read/v1 の RegisteredTool を持つ Run 境界を返す。"""

    registered = RegisteredTool(
        capability="document.read/v1",
        sdk_name="mcp__projectmind__document_read",
        provider="project",
        integration_id=None,
        input_schema={"type": "object"},
    )
    run_id = uuid4()
    root = Path("/tmp/projectmind-document-provider-tests") / str(run_id)
    return RunToolContext(
        run_id=run_id,
        run_attempt_id=uuid4(),
        project_id=project_id,
        user_id=uuid4(),
        tool=registered,
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
    )


def _validate_response(response: dict[str, object]) -> None:
    """Gateway が付与する evidence_refs を補い、公開 response Schema と照合する。"""

    payload = dict(response)
    payload["evidence_refs"] = ["ev_doc_001"]
    schema = ContractStore(CONTRACTS).load("tools/document.read/v1/response.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


async def test_reads_document_with_contract_valid_response_and_evidence() -> None:
    """所有 project の文書を読み、evidence_refs は付けず契約適合な response を返す。"""

    project_id = uuid4()
    provider = DocumentProvider(_FakeSource(project_id=project_id, content=_content()))

    result = await provider.execute(
        _context(project_id), {"path": "specs/overview.md", "purpose": "review"}
    )

    assert result.response["provider"] == "project"
    assert result.response["document"]["name"] == "overview.md"
    assert result.response["truncated"] is False
    # Evidence ref は Gateway が採番するため、Provider は付与しない。
    assert "evidence_refs" not in result.response
    assert result.evidence[0].content_hash == _CHECKSUM
    assert result.evidence[0].evidence_type == "document"
    _validate_response(dict(result.response))


async def test_line_range_selects_subset_and_reports_actual_end() -> None:
    """行範囲指定は該当行だけを返し、実際の終端行を報告する。"""

    project_id = uuid4()
    provider = DocumentProvider(_FakeSource(project_id=project_id, content=_content()))

    result = await provider.execute(
        _context(project_id),
        {"path": "specs/overview.md", "line_start": 2, "line_end": 2, "purpose": "x"},
    )

    assert result.response["content"] == "second line\n"
    assert result.response["line_start"] == 2
    assert result.response["line_end"] == 2


async def test_cross_project_read_is_folded_to_not_found() -> None:
    """他 project からの読み取りは存在を漏らさず not_found へ畳む。"""

    provider = DocumentProvider(_FakeSource(project_id=uuid4(), content=_content()))

    with pytest.raises(ToolProviderError) as excinfo:
        await provider.execute(_context(uuid4()), {"path": "specs/overview.md", "purpose": "x"})
    assert excinfo.value.code == "not_found"


async def test_path_traversal_is_invalid_request() -> None:
    """.. を含む path は文書解決前に invalid_request で拒否する。"""

    project_id = uuid4()
    provider = DocumentProvider(_FakeSource(project_id=project_id, content=_content()))

    with pytest.raises(ToolProviderError) as excinfo:
        await provider.execute(_context(project_id), {"path": "../secret.md", "purpose": "x"})
    assert excinfo.value.code == "invalid_request"


async def test_binary_document_is_invalid_request() -> None:
    """UTF-8 として読めない binary 文書は invalid_request で拒否する。"""

    project_id = uuid4()
    binary = _content(data=b"\x89PNG\r\n\x1a\n\x00", mime="image/png")
    provider = DocumentProvider(_FakeSource(project_id=project_id, content=binary))

    with pytest.raises(ToolProviderError) as excinfo:
        await provider.execute(_context(project_id), {"path": "specs/overview.md", "purpose": "x"})
    assert excinfo.value.code == "invalid_request"
