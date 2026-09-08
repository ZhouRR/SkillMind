"""実 git/svn client を本物の local repository に対して検証する (計画 §19 W4)。

外部 host は使わず、tmp_path 上に `git init` / `svnadmin create` で repository を作り、
`file://` で読む。command が無い環境では skip し、CI/開発機の差で偽陰性にならないようにする。
実 remote (HTTPS + 凭据) の疎通は本機で再現できないため、部署上の残余検証とする。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from projectmind.agent.repository_client import (
    GitCommandRepositoryClient,
    RepositoryClientError,
    RepositoryCredential,
    SvnCommandRepositoryClient,
)
from projectmind.agent.repository_source import (
    RepositoryBindingRef,
    RepositorySnapshotBinding,
    ScopedRepositorySession,
)
from projectmind.agent.workspace import WorkspaceManager
from projectmind.agent.workspace_materializer import WorkspaceMaterializer
from projectmind.agent.workspace_provider import WorkspaceSearchProvider
from projectmind.documents.snapshot import FrozenDocument
from projectmind.documents.source import ProjectDocumentContent
from tests.agent.input_fakes import TEST_BINDING_CHECKSUM, MemoryInputSnapshots, input_claim
from tests.agent.test_workspace_materializer import _read_context

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git command is not installed"
)
requires_svn = pytest.mark.skipif(
    shutil.which("svn") is None or shutil.which("svnadmin") is None,
    reason="svn commands are not installed",
)


class _EmptyInventory:
    """文書を持たない Project を表す inventory の fake。"""

    async def list_contents(
        self, *, project_id: UUID, documents: Sequence[FrozenDocument]
    ) -> list[ProjectDocumentContent]:
        """文書の無い Project を返す。"""

        del project_id, documents
        return []


class _DirectRepositorySource:
    """DB を介さず、固定 URI/revision/scope で実 client を開くテスト用 source。"""

    def __init__(self, client: object, *, uri: str, revision: str, scope_paths: tuple[str, ...]):
        """固定の接続先と scope を保持する。"""

        self._client = client
        self._uri = uri
        self._revision = revision
        self._scope_paths = scope_paths

    async def inspect(
        self, *, project_id: UUID, run_id: UUID, binding: RepositoryBindingRef
    ) -> RepositorySnapshotBinding:
        """DB の代役となる固定 metadata を、実 remote open と別の境界で返す。"""

        del project_id, run_id, binding
        return RepositorySnapshotBinding(
            checksum=TEST_BINDING_CHECKSUM, scope_paths=self._scope_paths
        )

    @asynccontextmanager
    async def open(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        binding: RepositoryBindingRef,
        requested_revision: str | None = None,
    ) -> AsyncIterator[ScopedRepositorySession]:
        """DB 解決を経ずに実 client の session を貸し出す。"""

        del project_id, run_id, binding
        client = self._client
        async with client.open(  # type: ignore[attr-defined]
            uri=self._uri,
            revision=requested_revision or self._revision,
            credential=None,
        ) as session:
            yield ScopedRepositorySession(
                session=session,
                scope_paths=self._scope_paths,
                binding_checksum=TEST_BINDING_CHECKSUM,
            )


def _run(arguments: list[str], *, home: Path) -> str:
    """テスト fixture 作成用に command を同期実行する。

    HOME は必ず tmp_path 配下へ向ける。既定の HOME を使うと svn/git が repository 内へ
    `.subversion` などの設定 directory を作り、作業樹を汚す。
    """

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    completed = subprocess.run(
        arguments,
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def _git_repository(root: Path) -> tuple[str, str]:
    """src/docs と symlink を含む git repository を作り、(URI, commit) を返す。"""

    origin = root / "origin"
    (origin / "src").mkdir(parents=True)
    (origin / "docs").mkdir(parents=True)
    source = "def main():\n    return 'ticket-42'\n"
    (origin / "src" / "app.py").write_text(source, encoding="utf-8")
    (origin / "src" / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    (origin / "docs" / "readme.md").write_text("# Doc\n", encoding="utf-8")
    (origin / "src" / "link.py").symlink_to("app.py")
    home = root / "home"
    home.mkdir()
    _run(["git", "init", "-q", "-b", "main", str(origin)], home=home)
    _run(["git", "-C", str(origin), "add", "-A"], home=home)
    _run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=ProjectMind Test",
            "-C",
            str(origin),
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        home=home,
    )
    commit = _run(["git", "-C", str(origin), "rev-parse", "HEAD"], home=home).strip()
    return f"file://{origin}", commit


def _svn_repository(root: Path) -> str:
    """src/docs を含む svn repository を作り、URI を返す。"""

    repository = root / "svnrepo"
    payload = root / "import"
    (payload / "src").mkdir(parents=True)
    (payload / "docs").mkdir(parents=True)
    source = "def main():\n    return 'ticket-42'\n"
    (payload / "src" / "app.py").write_text(source, encoding="utf-8")
    (payload / "docs" / "readme.md").write_text("# Doc\n", encoding="utf-8")
    home = root / "home"
    home.mkdir(exist_ok=True)
    _run(["svnadmin", "create", str(repository)], home=home)
    _run(
        ["svn", "import", "-q", "-m", "initial", str(payload), f"file://{repository}"],
        home=home,
    )
    return f"file://{repository}"


def _client(**overrides: int) -> GitCommandRepositoryClient:
    """既定 timeout の git client を作る。"""

    return GitCommandRepositoryClient(
        command_timeout_seconds=overrides.get("command_timeout_seconds", 60)
    )


@requires_git
@pytest.mark.asyncio
async def test_git_lists_and_reads_files_at_a_resolved_revision(tmp_path: Path) -> None:
    """`main` のような式は具体 commit へ解決され、blob の列挙と読取ができる。"""

    uri, commit = _git_repository(tmp_path)

    async with _client().open(uri=uri, revision="main", credential=None) as session:
        assert session.provider == "git"
        assert session.revision == commit
        listing = await session.list_files(["src"])
        content = await session.read_file("src/app.py", max_bytes=1_048_576)

    assert {entry.path for entry in listing.entries} == {"src/app.py", "src/blob.bin"}
    assert {entry.size for entry in listing.entries if entry.path == "src/app.py"} == {
        len("def main():\n    return 'ticket-42'\n")
    }
    # symlink は追わず、存在だけを skip として伝える。
    assert [(item.path, item.reason) for item in listing.skipped] == [("src/link.py", "symlink")]
    assert content.decode("utf-8").startswith("def main():")


@requires_git
@pytest.mark.asyncio
async def test_git_scope_paths_limit_the_listing(tmp_path: Path) -> None:
    """列挙は指定 path 配下だけを返し、repository 全体を落とさない。"""

    uri, _ = _git_repository(tmp_path)

    async with _client().open(uri=uri, revision="main", credential=None) as session:
        listing = await session.list_files(["docs"])

    assert {entry.path for entry in listing.entries} == {"docs/readme.md"}


@requires_git
@pytest.mark.asyncio
async def test_git_unknown_revision_fails_closed(tmp_path: Path) -> None:
    """未知 revision は空の結果ではなく明示的な失敗にする。"""

    uri, _ = _git_repository(tmp_path)

    with pytest.raises(RepositoryClientError) as error:
        async with _client().open(uri=uri, revision="no-such-branch", credential=None):
            pass

    assert error.value.code in {"not_found", "unavailable"}


@requires_git
@pytest.mark.asyncio
async def test_git_rejects_traversal_and_unsupported_uri(tmp_path: Path) -> None:
    """`..` path と ssh scheme は client 境界で拒否する。"""

    uri, _ = _git_repository(tmp_path)

    async with _client().open(uri=uri, revision="main", credential=None) as session:
        with pytest.raises(RepositoryClientError) as path_error:
            await session.read_file("../outside.txt", max_bytes=1_024)
    assert path_error.value.code == "invalid_request"

    with pytest.raises(RepositoryClientError) as uri_error:
        async with _client().open(
            uri="ssh://git@example.invalid/repo.git", revision="main", credential=None
        ):
            pass
    assert uri_error.value.code == "unavailable"


@requires_git
@pytest.mark.asyncio
async def test_git_read_limit_fails_closed(tmp_path: Path) -> None:
    """読取上限超過は截断せず too_large として拒否する。"""

    uri, _ = _git_repository(tmp_path)

    async with _client().open(uri=uri, revision="main", credential=None) as session:
        with pytest.raises(RepositoryClientError) as error:
            await session.read_file("src/app.py", max_bytes=4)

    assert error.value.code == "too_large"


@requires_git
@pytest.mark.asyncio
async def test_real_git_tree_is_materialized_and_discoverable(tmp_path: Path) -> None:
    """実 git repository を物化し、Agent が workspace.search で自走発見できる (§19 の核心)。"""

    uri, commit = _git_repository(tmp_path)
    workspace = WorkspaceManager((tmp_path / "runs").resolve()).initialize(uuid4())
    bindings = {
        "source_repository": RepositoryBindingRef(
            provider="git", integration_id=uuid4(), binding_id=uuid4()
        )
    }
    claim = input_claim(
        project_id=uuid4(), run_id=UUID(workspace.root.name), repository_bindings=bindings
    )
    materializer = WorkspaceMaterializer(
        document_inventory=_EmptyInventory(),
        input_snapshots=MemoryInputSnapshots(claim),
        max_bytes=10_485_760,
        max_files=500,
        repository_source=_DirectRepositorySource(
            _client(), uri=uri, revision="main", scope_paths=("src",)
        ),
    )

    prepared = await materializer.materialize(
        claimed_run=claim,
        workspace=workspace,
        project_id=claim.project_id,
        run_id=claim.run_id,
        blueprint={
            "resource_requirements": [
                {"key": "source_repository", "kind": "repository", "required": True}
            ]
        },
        repository_bindings=bindings,
    )
    workspace = prepared.workspace

    base = workspace.input_dir / "source_repository"
    manifest = json.loads((base / ".projectmind" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["revision"] == commit
    assert {item["path"] for item in manifest["files"]} == {"source_repository/src/app.py"}
    # 非 UTF-8 と symlink は「読めない」として残り、「存在しない」と誤認させない。
    assert {item["reason"] for item in manifest["skipped"]} == {"binary", "symlink"}

    found = await WorkspaceSearchProvider().execute(
        _read_context(workspace, "workspace.search/v1", project_id=claim.project_id),
        {"query": "ticket-42", "purpose": "Find the ticket reference in source"},
    )
    assert [item["path"] for item in found.response["matches"]] == [
        "input/source_repository/src/app.py"
    ]


@requires_svn
@pytest.mark.asyncio
async def test_svn_resolves_head_and_lists_directory_scope(tmp_path: Path) -> None:
    """`HEAD` は revision 番号へ解決され、directory scope 配下の file を列挙できる。"""

    uri = _svn_repository(tmp_path)
    client = SvnCommandRepositoryClient(command_timeout_seconds=60)

    async with client.open(uri=uri, revision="HEAD", credential=None) as session:
        assert session.provider == "svn"
        assert session.revision == "1"
        listing = await session.list_files(["src"])
        content = await session.read_file("src/app.py", max_bytes=1_048_576)

    assert {entry.path for entry in listing.entries} == {"src/app.py"}
    assert content.decode("utf-8").startswith("def main():")


@requires_svn
@pytest.mark.asyncio
async def test_svn_file_scope_and_missing_path_are_handled(tmp_path: Path) -> None:
    """file を直接指す scope はその path 自身を返し、存在しない scope は skip になる。"""

    uri = _svn_repository(tmp_path)
    client = SvnCommandRepositoryClient(command_timeout_seconds=60)

    async with client.open(uri=uri, revision="HEAD", credential=None) as session:
        listing = await session.list_files(["src/app.py", "missing"])

    assert [entry.path for entry in listing.entries] == ["src/app.py"]
    assert [(item.path, item.reason) for item in listing.skipped] == [("missing", "not_found")]


@requires_git
@pytest.mark.asyncio
async def test_git_history_is_scoped_to_the_bound_paths(tmp_path: Path) -> None:
    """履歴は scope path に触れた commit だけを新しい順に返す (計画 §19 W5)。"""

    uri, commit = _git_repository(tmp_path)
    home = tmp_path / "home"
    # scope 外 (docs/) だけを変更する 2 つ目の commit を積む。
    (tmp_path / "origin" / "docs" / "readme.md").write_text("# Doc v2\n", encoding="utf-8")
    _run(["git", "-C", str(tmp_path / "origin"), "add", "-A"], home=home)
    _run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=ProjectMind Test",
            "-C",
            str(tmp_path / "origin"),
            "commit",
            "-q",
            "-m",
            "docs only",
        ],
        home=home,
    )

    async with _client().open(uri=uri, revision="main", credential=None) as session:
        scoped = await session.read_history(["src"], limit=10)
        everything = await session.read_history(["src", "docs"], limit=10)

    assert [item.summary for item in scoped] == ["initial"]
    assert [item.revision for item in scoped] == [commit]
    assert scoped[0].author == "ProjectMind Test"
    # 新しい順で、scope を広げれば docs だけの commit も現れる。
    assert [item.summary for item in everything] == ["docs only", "initial"]


@requires_git
@pytest.mark.asyncio
async def test_git_history_respects_the_limit(tmp_path: Path) -> None:
    """件数上限を超えて履歴を持ち出さない。"""

    uri, _ = _git_repository(tmp_path)

    async with _client().open(uri=uri, revision="main", credential=None) as session:
        commits = await session.read_history(["src"], limit=1)

    assert len(commits) == 1


@requires_svn
@pytest.mark.asyncio
async def test_svn_history_merges_scope_paths_in_revision_order(tmp_path: Path) -> None:
    """svn も scope path ごとに引いた履歴を revision 降順へ併合する。"""

    uri = _svn_repository(tmp_path)
    _run(
        [
            "svn",
            "import",
            "-q",
            "-m",
            "add the release note",
            str(tmp_path / "import" / "docs" / "readme.md"),
            f"{uri}/src/notes.md",
        ],
        home=tmp_path / "home",
    )
    client = SvnCommandRepositoryClient(command_timeout_seconds=60)

    async with client.open(uri=uri, revision="HEAD", credential=None) as session:
        commits = await session.read_history(["src"], limit=10)

    assert [item.summary for item in commits] == ["add the release note", "initial"]
    assert [item.revision for item in commits] == ["2", "1"]


def test_credential_material_is_parsed_without_extra_configuration() -> None:
    """`user:token` は分割し、colon 無しの token は既定 user 名で HTTP Basic を組む。"""

    parsed = RepositoryCredential.from_material("deploy-user:pass:with:colon")
    assert parsed.username == "deploy-user"
    assert parsed.secret == "pass:with:colon"

    token = RepositoryCredential.from_material("ghp_exampletoken")
    assert token.username == "x-access-token"
    assert token.secret == "ghp_exampletoken"
    # secret は repr へ出さない (traceback/debug 出力からの漏洩面を作らない)。
    assert "ghp_exampletoken" not in repr(token)
