"""WorkspaceMaterializer の document/repository 物化、上限、安全境界を検証する (計画 §19 W3/W4)。

実 DB/object storage と実 Integration は本機で用意できないため、document inventory と
repository session を fake にして物化ロジック本体をオフラインで通す。実 git/svn command 経由の
物化は `test_repository_client.py` が本物の local repository に対して検証する。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from projectmind.agent.domain import RegisteredTool, RunWorkspace
from projectmind.agent.repository_client import (
    RepositoryClientError,
    RepositoryCommit,
    RepositoryFileEntry,
    RepositoryListing,
    RepositorySkippedEntry,
)
from projectmind.agent.repository_source import (
    RepositoryBindingRef,
    RepositorySnapshotBinding,
    ScopedRepositorySession,
)
from projectmind.agent.tool_gateway import RunToolContext
from projectmind.agent.workspace import WorkspaceManager
from projectmind.agent.workspace_materializer import (
    MaterializationError,
    WorkspaceMaterializer,
)
from projectmind.agent.workspace_provider import WorkspaceReadProvider, WorkspaceSearchProvider
from projectmind.core.hashing import sha256_hex
from projectmind.documents.snapshot import FrozenDocument
from projectmind.documents.source import ProjectDocumentContent
from projectmind.runs.domain import ClaimedRun
from projectmind.runs.input_snapshot import InputSnapshotStatus
from tests.agent.input_fakes import TEST_BINDING_CHECKSUM, MemoryInputSnapshots, input_claim
from tests.agent.test_binary_text import _workbook
from tests.documents.fakes import document_content, document_snapshot


class _FakeInventory:
    """固定の文書内容一覧を返す ProjectDocumentInventory の fake。"""

    def __init__(self, contents: Sequence[ProjectDocumentContent]) -> None:
        """固定内容と呼び出し回数を保持する。"""

        self._contents = tuple(contents)
        self.calls = 0

    async def list_contents(
        self, *, project_id: UUID, documents: Sequence[FrozenDocument]
    ) -> Sequence[ProjectDocumentContent]:
        """固定の文書内容一覧を返す。"""

        del project_id
        self.calls += 1
        selected = {item.document_id for item in documents}
        return tuple(item for item in self._contents if item.document_id in selected)


class _FakeRepositorySession:
    """固定 tree を返す RepositorySession の fake。"""

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        revision: str = "a" * 40,
        provider: str = "git",
        skipped: Sequence[RepositorySkippedEntry] = (),
        declared_sizes: dict[str, int] | None = None,
        commits: Sequence[RepositoryCommit] = (),
    ) -> None:
        """固定 tree・履歴・skip 一覧を保持する。"""

        self._files = dict(files)
        self._skipped = tuple(skipped)
        self._declared_sizes = dict(declared_sizes or {})
        self._commits = tuple(commits)
        self.provider = provider
        self.revision = revision
        self.history_limit: int | None = None
        self.reads: list[str] = []

    async def list_files(self, paths: Sequence[str]) -> RepositoryListing:
        """scope 内の固定 entry を列挙する。"""

        entries = tuple(
            RepositoryFileEntry(path=path, size=self._declared_sizes.get(path, len(data)))
            for path, data in sorted(self._files.items())
            if any(path == base or path.startswith(f"{base}/") for base in paths)
        )
        return RepositoryListing(entries=entries, skipped=self._skipped)

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """固定内容を返し、上限超過は too_large を送出する。"""

        self.reads.append(path)
        data = self._files.get(path)
        if data is None:
            raise RepositoryClientError("not_found", "missing", retryable=False)
        if len(data) > max_bytes:
            raise RepositoryClientError("too_large", "too large", retryable=False)
        return data

    async def read_history(
        self, paths: Sequence[str], *, limit: int
    ) -> tuple[RepositoryCommit, ...]:
        """上限付きで固定 commit を返す。"""

        del paths
        self.history_limit = limit
        return self._commits[:limit]


class _FakeRepositorySource:
    """凍結 binding を解決したことにして fake session を貸し出す source。"""

    def __init__(self, session: _FakeRepositorySession, *, scope_paths: tuple[str, ...]) -> None:
        """貸し出す session と scope を固定する。"""

        self._session = session
        self._scope_paths = scope_paths
        self.calls = 0
        self.inspections = 0
        self.checksum = TEST_BINDING_CHECKSUM

    async def inspect(
        self, *, project_id: UUID, run_id: UUID, binding: RepositoryBindingRef
    ) -> RepositorySnapshotBinding:
        """remote を開かない metadata 検査を、内容取得と別に数える。"""

        del project_id, run_id, binding
        self.inspections += 1
        return RepositorySnapshotBinding(checksum=self.checksum, scope_paths=self._scope_paths)

    @asynccontextmanager
    async def open(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        binding: RepositoryBindingRef,
        requested_revision: str | None = None,
    ) -> AsyncIterator[ScopedRepositorySession]:
        """凍結 binding を解決したことにして session を貸し出す。"""

        del project_id, run_id, binding, requested_revision
        self.calls += 1
        yield ScopedRepositorySession(
            session=self._session,
            scope_paths=self._scope_paths,
            binding_checksum=self.checksum,
        )


def _doc(
    folder: str, name: str, data: bytes, *, mime: str = "text/markdown"
) -> ProjectDocumentContent:
    """テスト用 ProjectDocumentContent を作る。checksum は content 由来で固定。"""

    return document_content(data, folder=folder, name=name, mime=mime)


_PROJECT_ID = uuid4()

_DOC_BLUEPRINT = {
    "resource_requirements": [
        {"key": "config", "kind": "document", "required": True, "access": "read"}
    ]
}
_REPOSITORY_BLUEPRINT = {
    "resource_requirements": [
        {"key": "source_repository", "kind": "repository", "required": True, "access": "read"}
    ]
}
_BINDING = RepositoryBindingRef(provider="git", integration_id=uuid4(), binding_id=uuid4())


def _materializer(
    claimed_run: ClaimedRun,
    contents: Sequence[ProjectDocumentContent],
    *,
    input_snapshots: MemoryInputSnapshots | None = None,
    repository_source: _FakeRepositorySource | None = None,
    **limits: int,
) -> WorkspaceMaterializer:
    """指定 inventory と上限で materializer を組む。既定上限は本番と同値。"""

    return WorkspaceMaterializer(
        document_inventory=_FakeInventory(contents),
        input_snapshots=input_snapshots or MemoryInputSnapshots(claimed_run),
        max_bytes=limits.get("max_bytes", 10_485_760),
        max_files=limits.get("max_files", 500),
        max_total_bytes=limits.get("max_total_bytes", 104_857_600),
        max_total_files=limits.get("max_total_files", 5_000),
        repository_source=repository_source,
    )


def _workspace(tmp_path: Path) -> RunWorkspace:
    """Run workspace を作る。"""

    return WorkspaceManager((tmp_path / "runs").resolve()).initialize(uuid4())


@pytest.mark.asyncio
async def test_materializes_explicit_frozen_documents_under_input_documents(tmp_path: Path) -> None:
    """明示的な凍結集合だけを input/documents/ 配下へ只読物化する。"""

    workspace = _workspace(tmp_path)
    contents = [
        _doc("specs", "design.md", b"# Design\n"),
        _doc("", "JAF-list.csv", b"key,owner\n1,alice\n"),
    ]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    base = workspace.input_dir / "documents"
    assert (base / "specs" / "design.md").read_text(encoding="utf-8") == "# Design\n"
    assert (base / "JAF-list.csv").read_text(encoding="utf-8") == "key,owner\n1,alice\n"
    manifest = json.loads((base / ".projectmind" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "project-documents"
    assert manifest["scope"] == {
        "document_ids": sorted(str(item.document_id) for item in contents),
        "requirements": {"config": document_snapshot(_PROJECT_ID, contents).to_json()},
    }
    assert manifest["materialized"] == {
        "files": 2,
        "bytes": len(b"# Design\n") + len(b"key,owner\n1,alice\n"),
    }
    assert {item["path"] for item in manifest["files"]} == {
        "documents/specs/design.md",
        "documents/JAF-list.csv",
    }
    assert manifest["skipped"] == []


@pytest.mark.asyncio
async def test_materialized_files_are_read_only(tmp_path: Path) -> None:
    """物化 file は 0o400 只読で、内容の原位改変を阻む (冻结证据)。"""

    workspace = _workspace(tmp_path)

    contents = [_doc("", "a.md", b"x")]
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    target = workspace.input_dir / "documents" / "a.md"
    assert (target.stat().st_mode & 0o777) == 0o400


@pytest.mark.asyncio
async def test_binary_and_oversize_documents_are_skipped_not_materialized(tmp_path: Path) -> None:
    """非 UTF-8 と per-file 上限超は skip し、manifest.skipped で存在を伝える。"""

    workspace = _workspace(tmp_path)
    contents = [
        _doc("", "ok.md", b"fine"),
        _doc("", "image.bin", b"\xff\xfe\x00", mime="application/octet-stream"),
        _doc("", "huge.txt", b"x" * 1_048_577),
    ]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    base = workspace.input_dir / "documents"
    assert (base / "ok.md").exists()
    assert not (base / "image.bin").exists()
    assert not (base / "huge.txt").exists()
    manifest = json.loads((base / ".projectmind" / "manifest.json").read_text(encoding="utf-8"))
    reasons = {item["path"]: item["reason"] for item in manifest["skipped"]}
    assert reasons == {
        "documents/image.bin": "binary",
        "documents/huge.txt": "exceeds_file_limit",
    }


@pytest.mark.asyncio
async def test_root_budget_overflow_never_publishes_partial_input(tmp_path: Path) -> None:
    """根ごとの上限超過は完成回执を作らず、候補を調査可能なまま保存する。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("", f"{i}.md", b"x" * 400) for i in range(5)]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    store = MemoryInputSnapshots(claim)
    with pytest.raises(MaterializationError, match="workspace budget"):
        await _materializer(claim, contents, input_snapshots=store, max_bytes=1_000).materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=_PROJECT_ID,
            run_id=UUID(workspace.root.name),
            blueprint=_DOC_BLUEPRINT,
            document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
        )

    assert not (workspace.input_dir / "documents").exists()
    assert store.record is not None and store.record.status is InputSnapshotStatus.PREPARING
    assert store.complete_calls == 0
    assert (workspace.root / ".projectmind-inputs" / str(store.record.snapshot_id)).is_dir()


@pytest.mark.asyncio
async def test_file_count_budget_overflow_fails_closed(tmp_path: Path) -> None:
    """件数上限超も fail closed する。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("", f"{i}.md", b"x") for i in range(4)]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    with pytest.raises(MaterializationError):
        prepared = await _materializer(claim, contents, max_files=3).materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=_PROJECT_ID,
            run_id=UUID(workspace.root.name),
            blueprint=_DOC_BLUEPRINT,
            document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
        )
        workspace = prepared.workspace


@pytest.mark.asyncio
async def test_no_resource_requirement_returns_receipted_empty_input(tmp_path: Path) -> None:
    """無入力も明示的な空回执で渡し、未検証 None と混同しない。"""

    workspace = _workspace(tmp_path)
    inventory = _FakeInventory([_doc("", "a.md", b"x")])
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
    )
    materializer = WorkspaceMaterializer(
        input_snapshots=MemoryInputSnapshots(claim),
        document_inventory=inventory,
        max_bytes=10_485_760,
        max_files=500,
    )

    prepared = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint={"resource_requirements": []},
    )
    workspace = prepared.workspace

    assert not (workspace.input_dir / "documents").exists()
    assert inventory.calls == 0
    assert prepared.resources == () and workspace.input_files == ()


@pytest.mark.asyncio
async def test_materialization_is_idempotent_per_run(tmp_path: Path) -> None:
    """既に物化済み (manifest 在中) なら再取得せず reuse する。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("", "a.md", b"first")]
    inventory = _FakeInventory(contents)
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    materializer = WorkspaceMaterializer(
        input_snapshots=MemoryInputSnapshots(claim),
        document_inventory=inventory,
        max_bytes=10_485_760,
        max_files=500,
    )
    project_id = _PROJECT_ID
    run_id = UUID(workspace.root.name)

    prepared = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=project_id,
        run_id=run_id,
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace
    prepared = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=project_id,
        run_id=run_id,
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    assert inventory.calls == 1


@pytest.mark.asyncio
async def test_tampered_materialization_fails_closed_on_reuse(tmp_path: Path) -> None:
    """再訪時に内容が manifest と食い違えば fail closed する (docs/06 §6.4 の checksum 検証)。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("", "a.md", b"first")]
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    materializer = _materializer(claim, contents)
    project_id = _PROJECT_ID
    run_id = UUID(workspace.root.name)
    prepared = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=project_id,
        run_id=run_id,
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    target = workspace.input_dir / "documents" / "a.md"
    target.chmod(0o600)
    target.write_bytes(b"tampered")

    with pytest.raises(MaterializationError):
        prepared = await materializer.materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=project_id,
            run_id=run_id,
            blueprint=_DOC_BLUEPRINT,
            document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
        )
        workspace = prepared.workspace


def _read_context(
    workspace: RunWorkspace, capability: str, *, project_id: UUID = _PROJECT_ID
) -> RunToolContext:
    """物化した workspace を読む read/search Tool snapshot を組む。"""

    return RunToolContext(
        run_id=UUID(workspace.root.name),
        run_attempt_id=uuid4(),
        project_id=project_id,
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


@pytest.mark.asyncio
async def test_materialized_documents_are_discoverable_via_workspace_tools(tmp_path: Path) -> None:
    """物化文書は既存の workspace.search/read で自走発見・精読できる (§19 の核心)。"""

    workspace = _workspace(tmp_path)
    contents = [
        _doc("specs", "design.md", b"# Design\n\nThe authorization boundary is here.\n"),
        _doc("", "notes.md", b"unrelated\n"),
    ]
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    found = await WorkspaceSearchProvider().execute(
        _read_context(workspace, "workspace.search/v1"),
        {"query": "authorization boundary", "purpose": "Find the design note"},
    )
    matches = found.response["matches"]
    assert [item["path"] for item in matches] == ["input/documents/specs/design.md"]

    read = await WorkspaceReadProvider().execute(
        _read_context(workspace, "workspace.read/v1"),
        {"path": "input/documents/specs/design.md", "purpose": "Read the design"},
    )
    assert "authorization boundary" in read.response["content"]


@pytest.mark.asyncio
async def test_unsafe_frozen_document_path_is_rejected_before_reading(tmp_path: Path) -> None:
    """不正な凍結 path は skip と偽装せず、inventory 取得前に拒否する。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("../escape", "x.md", b"nope"), _doc("", "ok.md", b"fine")]
    inventory = _FakeInventory(contents)
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    materializer = WorkspaceMaterializer(
        input_snapshots=MemoryInputSnapshots(claim),
        document_inventory=inventory,
        max_bytes=10_485_760,
        max_files=500,
    )
    with pytest.raises(MaterializationError, match="path is invalid"):
        prepared = await materializer.materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=_PROJECT_ID,
            run_id=UUID(workspace.root.name),
            blueprint=_DOC_BLUEPRINT,
            document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
        )
        workspace = prepared.workspace
    assert inventory.calls == 0
    assert not list(workspace.input_dir.iterdir())


@pytest.mark.asyncio
async def test_repository_tree_is_materialized_under_requirement_key(tmp_path: Path) -> None:
    """Repository は requirement_key 配下へ revision 固定で物化し、manifest に来歴を残す。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession(
        {"src/app.py": b"print('hi')\n", "src/util.py": b"# util\n", "docs/readme.md": b"# doc\n"},
        revision="b" * 40,
    )
    source = _FakeRepositorySource(session, scope_paths=("src",))

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    prepared = await _materializer(claim, [], repository_source=source).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_REPOSITORY_BLUEPRINT,
        repository_bindings={"source_repository": _BINDING},
    )
    workspace = prepared.workspace

    base = workspace.input_dir / "source_repository"
    assert (base / "src" / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    # scope 外 (docs/) は列挙対象に入らないため物化されない。
    assert not (base / "docs").exists()
    manifest = json.loads((base / ".projectmind" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "repository"
    assert manifest["provider"] == "git"
    assert manifest["revision"] == "b" * 40
    assert manifest["scope"] == {"paths": ["src"]}
    assert manifest["binding_id"] == str(_BINDING.binding_id)
    assert {item["path"] for item in manifest["files"]} == {
        "source_repository/src/app.py",
        "source_repository/src/util.py",
    }


@pytest.mark.asyncio
async def test_repository_symlink_binary_and_oversize_are_skipped(tmp_path: Path) -> None:
    """symlink/submodule/非 UTF-8/申告外過大は skip として manifest に残す。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession(
        {
            "src/app.py": b"ok\n",
            "src/image.bin": b"\xff\xfe\x00",
            "src/huge.txt": b"x" * 1_048_577,
        },
        skipped=(
            RepositorySkippedEntry(path="src/link.py", reason="symlink"),
            RepositorySkippedEntry(path="src/vendor", reason="submodule"),
        ),
        # server 申告が実体より小さい場合でも読取上限で止め、skip へ回す。
        declared_sizes={"src/huge.txt": 10},
    )
    source = _FakeRepositorySource(session, scope_paths=("src",))

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    prepared = await _materializer(claim, [], repository_source=source).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_REPOSITORY_BLUEPRINT,
        repository_bindings={"source_repository": _BINDING},
    )
    workspace = prepared.workspace

    base = workspace.input_dir / "source_repository"
    manifest = json.loads((base / ".projectmind" / "manifest.json").read_text(encoding="utf-8"))
    reasons = {item["path"]: item["reason"] for item in manifest["skipped"]}
    assert reasons == {
        "source_repository/src/link.py": "symlink",
        "source_repository/src/vendor": "submodule",
        "source_repository/src/image.bin": "binary",
        "source_repository/src/huge.txt": "exceeds_file_limit",
    }
    assert (base / "src" / "app.py").exists()
    assert not (base / "src" / "image.bin").exists()


@pytest.mark.asyncio
async def test_repository_budget_overflow_fails_closed_before_reading(tmp_path: Path) -> None:
    """列挙時に分かる上限超過なら本文を取らず、候補を完成扱いにしない。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession({f"src/{i}.py": b"x" * 400 for i in range(5)})
    source = _FakeRepositorySource(session, scope_paths=("src",))

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    with pytest.raises(MaterializationError):
        prepared = await _materializer(
            claim, [], repository_source=source, max_bytes=1_000
        ).materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=_PROJECT_ID,
            run_id=UUID(workspace.root.name),
            blueprint=_REPOSITORY_BLUEPRINT,
            repository_bindings={"source_repository": _BINDING},
        )
        workspace = prepared.workspace

    assert not (workspace.input_dir / "source_repository").exists()
    assert session.reads == []


@pytest.mark.asyncio
async def test_repository_materialization_without_source_fails_closed(tmp_path: Path) -> None:
    """未配線環境で repository binding が来たら、空の樹を渡さず fail closed する。"""

    workspace = _workspace(tmp_path)

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    with pytest.raises(MaterializationError):
        prepared = await _materializer(claim, []).materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=_PROJECT_ID,
            run_id=UUID(workspace.root.name),
            blueprint=_REPOSITORY_BLUEPRINT,
            repository_bindings={"source_repository": _BINDING},
        )
        workspace = prepared.workspace


@pytest.mark.asyncio
async def test_repository_requirement_key_cannot_escape_or_shadow_documents(
    tmp_path: Path,
) -> None:
    """外部 Skill 由来の key で input/ を逃げたり documents/ を上書きしたりできない。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession({"src/app.py": b"ok\n"})
    source = _FakeRepositorySource(session, scope_paths=("src",))
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    materializer = _materializer(claim, [], repository_source=source)

    for key in ("../escape", "documents", "."):
        with pytest.raises(MaterializationError):
            prepared = await materializer.materialize(
                claimed_run=claim,
                workspace=workspace,
                project_id=_PROJECT_ID,
                run_id=UUID(workspace.root.name),
                blueprint=_REPOSITORY_BLUEPRINT,
                repository_bindings={key: _BINDING},
            )
            workspace = prepared.workspace
    assert not (workspace.input_dir.parent / "escape").exists()


@pytest.mark.asyncio
async def test_repository_materialization_is_idempotent_per_run(tmp_path: Path) -> None:
    """Repository も Run 単位で 1 回だけ物化し、再訪は検証して reuse する。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession({"src/app.py": b"ok\n"})
    source = _FakeRepositorySource(session, scope_paths=("src",))
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    materializer = _materializer(claim, [], repository_source=source)
    project_id = _PROJECT_ID
    run_id = UUID(workspace.root.name)

    for _ in range(2):
        prepared = await materializer.materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=project_id,
            run_id=run_id,
            blueprint=_REPOSITORY_BLUEPRINT,
            repository_bindings={"source_repository": _BINDING},
        )
        workspace = prepared.workspace

    assert source.calls == 1
    assert source.inspections == 2


@pytest.mark.asyncio
async def test_file_index_lists_materialized_and_skipped_entries(tmp_path: Path) -> None:
    """索引 file が物化物と skip を併記し、名前での発見を成立させる (計画 §19 W5)。"""

    workspace = _workspace(tmp_path)
    contents = [
        _doc("specs", "design.md", b"# Design\n"),
        _doc("", "image.bin", b"\xff\xfe\x00", mime="application/octet-stream"),
    ]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    index = (workspace.input_dir / "documents" / ".projectmind" / "files.txt").read_text(
        encoding="utf-8"
    )
    assert "specs/design.md" in index
    # 読めなかった原本も名前で見つかる (「存在しない」と誤認させない)。
    assert "# skipped\timage.bin\tbinary" in index
    manifest = json.loads(
        (workspace.input_dir / "documents" / ".projectmind" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert [item["path"] for item in manifest["generated"]] == ["documents/.projectmind/files.txt"]


@pytest.mark.asyncio
async def test_spreadsheet_documents_are_textualized_and_traceable(tmp_path: Path) -> None:
    """xlsx 設計書は平台側で text 化し、原本 path と hash を manifest に残す (計画 §19 W5)。"""

    workspace = _workspace(tmp_path)
    body = '<row r="1"><c r="A1" t="inlineStr"><is><t>認証境界の設計</t></is></c></row>'
    workbook = _workbook([("設計", body)])
    contents = [_doc("specs", "design.xlsx", workbook, mime="application/vnd.ms-excel")]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    base = workspace.input_dir / "documents"
    rendered = (base / "specs" / "design.xlsx.txt").read_text(encoding="utf-8")
    assert "## Sheet: 設計" in rendered
    assert "A1: 認証境界の設計" in rendered
    # 原本 (binary) 自体は物化しない。読めない byte 列を予算に載せる意味が無い。
    assert not (base / "specs" / "design.xlsx").exists()
    manifest = json.loads((base / ".projectmind" / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"].endswith(".xlsx.txt"))
    assert entry["converted_from"] == "documents/specs/design.xlsx"
    assert entry["source_content_hash"] == f"sha256:{sha256_hex(workbook)}"
    assert manifest["skipped"] == []

    found = await WorkspaceSearchProvider().execute(
        _read_context(workspace, "workspace.search/v1"),
        {"query": "認証境界", "purpose": "Find the design cell"},
    )
    assert [item["path"] for item in found.response["matches"]] == [
        "input/documents/specs/design.xlsx.txt"
    ]


@pytest.mark.asyncio
async def test_unconvertible_spreadsheet_is_skipped_with_reason(tmp_path: Path) -> None:
    """xlsx を名乗る壊れた file は「読めない」として残し、存在を隠さない。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("specs", "broken.xlsx", b"\xff\xfe\x00not a workbook")]

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    prepared = await _materializer(claim, contents).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    manifest = json.loads(
        (workspace.input_dir / "documents" / ".projectmind" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["path"]: item["reason"] for item in manifest["skipped"]} == {
        "documents/specs/broken.xlsx": "unconvertible_document"
    }


@pytest.mark.asyncio
async def test_repository_history_is_materialized_with_its_limit(tmp_path: Path) -> None:
    """Repository 履歴を text として物化し、件数上限を text 自身に書く (計画 §19 W5)。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession(
        {"src/app.py": b"ok\n"},
        commits=(
            RepositoryCommit(
                revision="b" * 40,
                committed_at="2026-07-24T10:00:00+09:00",
                author="Tester",
                summary="Fix the authorization boundary",
            ),
        ),
    )
    source = _FakeRepositorySource(session, scope_paths=("src",))

    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    prepared = await _materializer(claim, [], repository_source=source).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=_REPOSITORY_BLUEPRINT,
        repository_bindings={"source_repository": _BINDING},
    )
    workspace = prepared.workspace

    history = (
        workspace.input_dir / "source_repository" / ".projectmind" / "history.txt"
    ).read_text(encoding="utf-8")
    assert history.splitlines()[0].startswith("# most recent 1 commits")
    assert "Fix the authorization boundary" in history
    assert session.history_limit == 200
    manifest = json.loads(
        (workspace.input_dir / "source_repository" / ".projectmind" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert {item["path"] for item in manifest["generated"]} == {
        "source_repository/.projectmind/files.txt",
        "source_repository/.projectmind/history.txt",
    }


@pytest.mark.asyncio
async def test_generated_artifacts_are_verified_on_reuse(tmp_path: Path) -> None:
    """索引・履歴も再訪時に hash 検証する (生成物だけ差し替えられる余地を残さない)。"""

    workspace = _workspace(tmp_path)
    contents = [_doc("", "a.md", b"first")]
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    materializer = _materializer(claim, contents)
    project_id = _PROJECT_ID
    run_id = UUID(workspace.root.name)
    prepared = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=project_id,
        run_id=run_id,
        blueprint=_DOC_BLUEPRINT,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
    )
    workspace = prepared.workspace

    index = workspace.input_dir / "documents" / ".projectmind" / "files.txt"
    index.chmod(0o600)
    index.write_text("b.md\n", encoding="utf-8")

    with pytest.raises(MaterializationError):
        prepared = await materializer.materialize(
            claimed_run=claim,
            workspace=workspace,
            project_id=project_id,
            run_id=run_id,
            blueprint=_DOC_BLUEPRINT,
            document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
        )
        workspace = prepared.workspace


@pytest.mark.asyncio
async def test_materialize_returns_descriptors_for_brief_and_prompt(tmp_path: Path) -> None:
    """物化結果として落点記述子を返す。Brief/prompt はこれだけを材料にする (計画 §19 W6)。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession(
        {"src/app.py": b"ok\n"},
        revision="c" * 40,
        commits=(
            RepositoryCommit(
                revision="c" * 40,
                committed_at="2026-07-25T09:00:00+09:00",
                author="Tester",
                summary="initial",
            ),
        ),
    )
    source = _FakeRepositorySource(session, scope_paths=("src",))
    blueprint = {
        "resource_requirements": [
            {"key": "config", "kind": "document", "required": True, "access": "read"},
            {"key": "source_repository", "kind": "repository", "required": True, "access": "read"},
        ]
    }

    contents = [_doc("", "a.md", b"x"), _doc("", "image.bin", b"\xff\xfe\x00")]
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        snapshots=(document_snapshot(_PROJECT_ID, contents),),
        repository_bindings={"source_repository": _BINDING},
    )
    materialized = await _materializer(
        claim,
        contents,
        repository_source=source,
    ).materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        blueprint=blueprint,
        document_snapshots=(document_snapshot(_PROJECT_ID, contents),),
        repository_bindings={"source_repository": _BINDING},
    )

    workspace = materialized.workspace
    documents, repository = materialized.resources
    assert (documents.kind, documents.root) == ("document", "input/documents")
    assert documents.index_path == "input/documents/.projectmind/files.txt"
    assert documents.manifest_path == "input/documents/.projectmind/manifest.json"
    # document 一路に履歴は無い。無い path を案内すると Agent が読めない file を試す。
    assert documents.history_path is None
    assert (documents.files, documents.skipped) == (1, 1)
    assert repository.requirement_key == "source_repository"
    assert repository.revision == "c" * 40
    assert repository.history_path == "input/source_repository/.projectmind/history.txt"
    assert repository.files == 1
    # 論理 path の案内は受け取った世代へ解決でき、実 Tool から読める。
    for descriptor in materialized.resources:
        for relative in (descriptor.manifest_path, descriptor.index_path):
            target = workspace.input_dir / relative.removeprefix("input/")
            assert target.is_file()
            read = await WorkspaceReadProvider().execute(
                _read_context(workspace, "workspace.read/v1"),
                {"path": relative, "purpose": "Read the prepared input guide"},
            )
            assert read.response["content"] == target.read_text()


@pytest.mark.asyncio
async def test_reused_materialization_returns_the_same_descriptors(tmp_path: Path) -> None:
    """再試行 (reuse 経路) でも同じ落点を返し、初回と案内が食い違わない。"""

    workspace = _workspace(tmp_path)
    session = _FakeRepositorySession({"src/app.py": b"ok\n"}, revision="d" * 40)
    source = _FakeRepositorySource(session, scope_paths=("src",))
    claim = input_claim(
        project_id=_PROJECT_ID,
        run_id=UUID(workspace.root.name),
        repository_bindings={"source_repository": _BINDING},
    )
    materializer = _materializer(claim, [], repository_source=source)
    project_id = _PROJECT_ID
    run_id = UUID(workspace.root.name)

    first = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=project_id,
        run_id=run_id,
        blueprint=_REPOSITORY_BLUEPRINT,
        repository_bindings={"source_repository": _BINDING},
    )
    second = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=project_id,
        run_id=run_id,
        blueprint=_REPOSITORY_BLUEPRINT,
        repository_bindings={"source_repository": _BINDING},
    )

    assert first == second
    assert source.calls == 1
