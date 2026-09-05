"""Workspace read/search Provider の path、resource limit、contract 境界を検証する。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.agent.context_builder import ContractStore
from projectmind.agent.domain import RegisteredTool
from projectmind.agent.tool_gateway import RunToolContext, ToolProviderError
from projectmind.agent.workspace import WorkspaceManager
from projectmind.agent.workspace_provider import (
    WorkspaceReadProvider,
    WorkspaceSearchProvider,
    WorkspaceWriteProvider,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


def _context(tmp_path: Path, capability: str) -> RunToolContext:
    """実 directory を持つ一 Run と workspace Tool snapshot を組み立てる。"""

    run_id = uuid4()
    workspace = WorkspaceManager((tmp_path / "runs").resolve()).initialize(run_id)
    return RunToolContext(
        run_id=run_id,
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        tool=RegisteredTool(
            capability=capability,
            sdk_name=f"mcp__projectmind__{capability.replace('.', '_').replace('/', '_')}",
            provider="workspace",
            integration_id=None,
            input_schema={"type": "object"},
        ),
        workspace=workspace,
    )


def _validate_response(path: str, response: dict[str, object]) -> None:
    """Gateway 採番の Evidence ref を補い、公開 response contract へ照合する。"""

    payload = dict(response)
    payload["evidence_refs"] = ["ev_workspace_001"]
    schema = ContractStore(CONTRACTS).load(path)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


@pytest.mark.asyncio
async def test_read_returns_bounded_utf8_content_and_evidence(tmp_path: Path) -> None:
    """input 内 file の行範囲、全体 hash、locator を契約適合形で返す。"""

    context = _context(tmp_path, "workspace.read/v1")
    target = context.workspace.input_dir / "repository" / "src" / "example.py"
    target.parent.mkdir(parents=True)
    target.write_text("first\nauthorize(actor)\nthird\n", encoding="utf-8")

    result = await WorkspaceReadProvider().execute(
        context,
        {
            "path": "input/repository/src/example.py",
            "line_start": 2,
            "line_end": 2,
            "purpose": "Inspect authorization",
        },
    )

    _validate_response("tools/workspace.read/v1/response.schema.json", dict(result.response))
    assert result.response["content"] == "authorize(actor)\n"
    assert result.evidence[0].source_uri.startswith("workspace://runs/")
    assert result.evidence[0].source_locator["path"] == "input/repository/src/example.py"


@pytest.mark.asyncio
async def test_read_rejects_traversal_symlink_binary_and_large_file(tmp_path: Path) -> None:
    """Root 逃逸、symlink、非 UTF-8 と per-file 上限を Provider 呼出し前後で閉じる。"""

    context = _context(tmp_path, "workspace.read/v1")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = context.workspace.input_dir / "link.txt"
    link.symlink_to(outside)
    binary = context.workspace.input_dir / "binary.dat"
    binary.write_bytes(b"\xff\xfe")
    large = context.workspace.input_dir / "large.txt"
    large.write_bytes(b"x" * 1_048_577)

    cases = (
        ("input/../outside.txt", "invalid_request"),
        ("input/link.txt", "invalid_request"),
        ("input/binary.dat", "invalid_request"),
        ("input/large.txt", "too_large"),
    )
    for path, code in cases:
        with pytest.raises(ToolProviderError) as captured:
            await WorkspaceReadProvider().execute(
                context,
                {"path": path, "purpose": "Boundary test"},
            )
        assert captured.value.code == code


@pytest.mark.asyncio
async def test_search_is_literal_bounded_and_skips_non_utf8_files(tmp_path: Path) -> None:
    """検索は regex でなく literal とし、結果上限と非 UTF-8 skip を監査可能に返す。"""

    context = _context(tmp_path, "workspace.search/v1")
    source = context.workspace.cwd / "src"
    source.mkdir()
    (source / "one.py").write_text("authorize(a)\nauthorize(b)\n", encoding="utf-8")
    (source / "two.py").write_text("[a-z] is literal\n", encoding="utf-8")
    (source / "binary.dat").write_bytes(b"\xff")

    result = await WorkspaceSearchProvider().execute(
        context,
        {
            "query": "authorize(",
            "paths": ["workspace/src"],
            "case_sensitive": True,
            "max_results": 1,
            "purpose": "Find authorization calls",
        },
    )

    _validate_response("tools/workspace.search/v1/response.schema.json", dict(result.response))
    assert len(result.response["matches"]) == 1
    assert result.response["truncated"] is True
    assert result.evidence[0].source_locator["path"] == "workspace/src/one.py"

    literal = await WorkspaceSearchProvider().execute(
        context,
        {"query": "[a-z]", "paths": ["workspace/src"], "purpose": "Literal search"},
    )
    assert len(literal.response["matches"]) == 1


@pytest.mark.asyncio
async def test_search_without_matches_still_returns_redacted_audit_evidence(
    tmp_path: Path,
) -> None:
    """0 件検索も query 本文を保存せず checksum だけの Evidence を返す。"""

    context = _context(tmp_path, "workspace.search/v1")
    (context.workspace.input_dir / "empty.txt").write_text("nothing", encoding="utf-8")

    result = await WorkspaceSearchProvider().execute(
        context,
        {"query": "missing-value", "purpose": "Confirm absence"},
    )

    assert result.response["matches"] == []
    assert result.evidence[0].evidence_type == "workspace-search"
    assert "query" not in result.evidence[0].source_locator
    assert str(result.evidence[0].source_locator["query_hash"]).startswith("sha256:")


@pytest.mark.asyncio
async def test_write_persists_output_file_with_hash_and_evidence(tmp_path: Path) -> None:
    """output/ への UTF-8 書き込みが実 file を作り、契約適合 response と Evidence を返す。"""

    context = _context(tmp_path, "workspace.write/v1")
    body = "# Review summary\n\n- Boundary looks correct.\n"

    result = await WorkspaceWriteProvider().execute(
        context,
        {"path": "output/review.md", "content": body, "purpose": "Persist the report"},
    )

    _validate_response("tools/workspace.write/v1/response.schema.json", dict(result.response))
    assert result.response["created"] is True
    assert result.response["bytes_written"] == len(body.encode("utf-8"))
    assert result.response["path"] == "output/review.md"
    written = context.workspace.output_dir / "review.md"
    assert written.read_text(encoding="utf-8") == body
    assert result.evidence[0].evidence_type == "workspace-write"
    assert result.evidence[0].metadata["read_only"] is False


@pytest.mark.asyncio
async def test_write_creates_nested_dirs_and_overwrite_reports_not_created(
    tmp_path: Path,
) -> None:
    """未存在の親 directory を作り、再書き込みは created=false で上書きする。"""

    context = _context(tmp_path, "workspace.write/v1")

    first = await WorkspaceWriteProvider().execute(
        context,
        {"path": "workspace/notes/draft.txt", "content": "one", "purpose": "draft"},
    )
    assert first.response["created"] is True

    second = await WorkspaceWriteProvider().execute(
        context,
        {"path": "workspace/notes/draft.txt", "content": "two", "purpose": "revise"},
    )
    assert second.response["created"] is False
    target = context.workspace.cwd / "notes" / "draft.txt"
    assert target.read_text(encoding="utf-8") == "two"


@pytest.mark.asyncio
async def test_write_rejects_input_root(tmp_path: Path) -> None:
    """物化済み input/ は冻结证据であり書き込み対象にならない (計画 §19 W2)。"""

    context = _context(tmp_path, "workspace.write/v1")

    with pytest.raises(ToolProviderError) as failure:
        await WorkspaceWriteProvider().execute(
            context,
            {"path": "input/repository/x.py", "content": "nope", "purpose": "escape"},
        )
    assert failure.value.code == "invalid_request"
    assert not (context.workspace.input_dir / "repository" / "x.py").exists()


@pytest.mark.asyncio
async def test_write_rejects_traversal_and_bare_root(tmp_path: Path) -> None:
    """`..` 越境と裸 root への書き込みを拒否する。"""

    context = _context(tmp_path, "workspace.write/v1")

    for path in ("workspace/../escape.txt", "output"):
        with pytest.raises(ToolProviderError) as failure:
            await WorkspaceWriteProvider().execute(
                context,
                {"path": path, "content": "x", "purpose": "escape"},
            )
        assert failure.value.code == "invalid_request"
    assert not (context.workspace.root.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_write_rejects_symlink_in_path(tmp_path: Path) -> None:
    """既存 symlink を辿る書き込みを拒否し、外部へ書き出さない。"""

    context = _context(tmp_path, "workspace.write/v1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (context.workspace.cwd / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ToolProviderError) as failure:
        await WorkspaceWriteProvider().execute(
            context,
            {"path": "workspace/link/pwned.txt", "content": "x", "purpose": "escape"},
        )
    assert failure.value.code == "invalid_request"
    assert not (outside / "pwned.txt").exists()


@pytest.mark.asyncio
async def test_write_rejects_content_over_limit(tmp_path: Path) -> None:
    """単 file 上限超は too_large で拒否し、部分書き込みを残さない。"""

    context = _context(tmp_path, "workspace.write/v1")

    with pytest.raises(ToolProviderError) as failure:
        await WorkspaceWriteProvider().execute(
            context,
            {
                "path": "output/big.txt",
                "content": "x" * 1_048_577,
                "purpose": "overflow",
            },
        )
    assert failure.value.code == "too_large"
    assert not (context.workspace.output_dir / "big.txt").exists()
