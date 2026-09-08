"""DocumentProvider の project 作用域と document.read/v1 契約適合を検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.agent.context_builder import ContractStore
from projectmind.agent.document_provider import DocumentProvider
from projectmind.agent.domain import RegisteredTool, RunContext, RunLimits, RunWorkspace
from projectmind.agent.tool_gateway import RunToolContext, ToolProviderError
from projectmind.documents.source import ProjectDocumentContent
from tests.documents.fakes import document_content, document_snapshot

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
_CHECKSUM = document_content().checksum


class _FakeSource:
    """指定 project でのみ 1 文書を返す in-memory な ProjectDocumentSource。"""

    def __init__(self, *, project_id: UUID, content: ProjectDocumentContent) -> None:
        """所有 project と返す文書内容を保持する。"""

        self._project_id = project_id
        self._content = content
        self.calls: list[UUID] = []

    async def fetch(self, *, project_id: UUID, document_id: UUID) -> ProjectDocumentContent | None:
        """所有 Project と凍結 ID が一致する場合だけ内容を返す。"""

        self.calls.append(document_id)
        if project_id != self._project_id:
            return None
        if document_id == self._content.document_id:
            return self._content
        return None


def _content(
    data: bytes = b"# Overview\nsecond line\n", *, mime: str = "text/markdown"
) -> ProjectDocumentContent:
    """検証用の文書内容を組み立てる。"""

    return document_content(data, mime=mime)


def _context(project_id: UUID, *, content: ProjectDocumentContent | None = None) -> RunToolContext:
    """document.read/v1 の RegisteredTool を持つ Run 境界を返す。"""

    registered = RegisteredTool(
        capability="document.read/v1",
        sdk_name="mcp__projectmind__document_read",
        provider="project-documents",
        integration_id=None,
        input_schema={"type": "object"},
    )
    run_id = uuid4()
    attempt_id, user_id = uuid4(), uuid4()
    root = Path("/tmp/projectmind-document-provider-tests") / str(run_id)
    workspace = RunWorkspace(
        root=root,
        cwd=root / "workspace",
        input_dir=root / "input",
        output_dir=root / "output",
        temp_dir=root / "temp",
    )
    run = RunContext(
        run_id=run_id,
        run_attempt_id=attempt_id,
        project_id=project_id,
        user_id=user_id,
        prompt="Read the frozen document",
        task_snapshot={},
        skill_snapshots=(),
        resolved_sources={
            "config": {
                "capability": "document.read/v1",
                "provider": "project-documents",
                "document_snapshot": document_snapshot(
                    project_id, [content or _content()]
                ).to_json(),
            }
        },
        permission_snapshot={"allowed_capabilities": ["document.read/v1"]},
        workspace=workspace,
        limits=RunLimits(20, 900, 1_048_576),
        result_schema={"type": "object"},
        tools=(registered,),
        model="test",
    )
    return RunToolContext(
        run_id=run_id,
        run_attempt_id=attempt_id,
        project_id=project_id,
        user_id=user_id,
        tool=registered,
        workspace=workspace,
        run=run,
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


async def test_missing_frozen_document_is_unavailable() -> None:
    """凍結済み ID が取得できなければ、新しい内容へ切り替えず unavailable となる。"""

    provider = DocumentProvider(_FakeSource(project_id=uuid4(), content=_content()))

    with pytest.raises(ToolProviderError) as excinfo:
        await provider.execute(_context(uuid4()), {"path": "specs/overview.md", "purpose": "x"})
    assert excinfo.value.code == "unavailable"


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
        await provider.execute(
            _context(project_id, content=binary), {"path": "specs/overview.md", "purpose": "x"}
        )
    assert excinfo.value.code == "invalid_request"


async def test_unselected_path_is_rejected_without_source_lookup() -> None:
    """同 Project に存在する未選択文書の有無も Provider へ問い合わせない。"""

    project_id = uuid4()
    hidden = document_content(name="unselected.md")
    source = _FakeSource(project_id=project_id, content=hidden)
    with pytest.raises(ToolProviderError) as error:
        await DocumentProvider(source).execute(
            _context(project_id), {"path": "specs/unselected.md", "purpose": "x"}
        )
    assert error.value.code == "not_found"
    assert source.calls == []


@pytest.mark.parametrize("change", ["identity", "bytes", "hash", "size"])
async def test_reuploaded_or_changed_document_is_not_returned(change: str) -> None:
    """同名の別 ID・本文・申告 metadata の差し替えを検出する。"""

    project_id = uuid4()
    original = _content()
    changed = {
        "identity": replace(original, document_id=uuid4()),
        "bytes": replace(original, data=b"changed without changing declared metadata"),
        "hash": replace(original, checksum="sha256:" + "b" * 64),
        "size": replace(original, size=original.size + 1),
    }[change]
    with pytest.raises(ToolProviderError) as error:
        await DocumentProvider(_FakeSource(project_id=project_id, content=changed)).execute(
            _context(project_id), {"path": "specs/overview.md", "purpose": "x"}
        )
    assert error.value.code == "unavailable"


@pytest.mark.parametrize("change", ["missing", "project", "attempt", "actor"])
async def test_foreign_or_missing_run_context_is_denied_before_lookup(change: str) -> None:
    """Project/Attempt/actor の違う context を別 Run の認可に転用できない。"""

    project_id = uuid4()
    context = _context(project_id)
    assert context.run is not None
    run = {
        "missing": None,
        "project": replace(context.run, project_id=uuid4()),
        "attempt": replace(context.run, run_attempt_id=uuid4()),
        "actor": replace(context.run, user_id=uuid4()),
    }[change]
    source = _FakeSource(project_id=project_id, content=_content())
    with pytest.raises(ToolProviderError) as error:
        await DocumentProvider(source).execute(
            replace(context, run=run), {"path": "specs/overview.md", "purpose": "x"}
        )
    assert error.value.code == "scope_denied"
    assert source.calls == []
