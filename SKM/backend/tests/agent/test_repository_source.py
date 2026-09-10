"""凍結 binding の scope/revision 強制を検証する (計画 §19 W4)。

Integration 行の解決は実 DB 検証 (`tests/db/test_real_database_invariants.py`) が担い、ここでは
「binding が許した範囲の外へ出られない」判定そのものを対象にする。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from skillmind.agent.repository_client import (
    RepositoryClientError,
    RepositoryFileEntry,
    RepositoryListing,
)
from skillmind.agent.repository_source import (
    ScopedRepositorySession,
    path_within_scope,
    select_frozen_revision,
)


class _RecordingSession:
    """委譲された引数を記録するだけの RepositorySession の fake。"""

    def __init__(self) -> None:
        """呼び出し記録を初期化する。"""

        self.provider = "git"
        self.revision = "c" * 40
        self.listed: list[tuple[str, ...]] = []
        self.read: list[str] = []

    async def list_files(self, paths: Sequence[str]) -> RepositoryListing:
        """要求 path を記録して固定 entry を返す。"""

        self.listed.append(tuple(paths))
        return RepositoryListing(
            entries=(RepositoryFileEntry(path="src/app.py", size=3),), skipped=()
        )

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """読み出し path を記録して固定内容を返す。"""

        del max_bytes
        self.read.append(path)
        return b"ok\n"


def _scoped(session: _RecordingSession) -> ScopedRepositorySession:
    """`src` と `docs/spec.md` だけを許す scope 付き session を作る。"""

    return ScopedRepositorySession(session=session, scope_paths=("src", "docs/spec.md"))


@pytest.mark.asyncio
async def test_default_listing_uses_the_frozen_scope_paths() -> None:
    """path 未指定の列挙は binding が凍結した scope 全体を対象にする。"""

    session = _RecordingSession()

    await _scoped(session).list_files()

    assert session.listed == [("src", "docs/spec.md")]


@pytest.mark.asyncio
async def test_reads_inside_the_scope_are_delegated() -> None:
    """Scope 配下の path と scope path 自身は読める。"""

    session = _RecordingSession()
    scoped = _scoped(session)

    await scoped.read_file("src/deep/module.py", max_bytes=1_024)
    await scoped.read_file("docs/spec.md", max_bytes=1_024)

    assert session.read == ["src/deep/module.py", "docs/spec.md"]


@pytest.mark.asyncio
async def test_reads_outside_the_scope_are_refused_before_delegation() -> None:
    """Scope 外 path は委譲前に拒否する (取得してから捨てない)。"""

    session = _RecordingSession()

    with pytest.raises(RepositoryClientError) as error:
        await _scoped(session).read_file("secrets/id_rsa", max_bytes=1_024)

    assert error.value.code == "invalid_request"
    assert session.read == []


@pytest.mark.asyncio
async def test_listing_outside_the_scope_is_refused() -> None:
    """明示 path 指定でも scope 外は列挙できない。"""

    session = _RecordingSession()

    with pytest.raises(RepositoryClientError):
        await _scoped(session).list_files(["/etc"])

    assert session.listed == []


def test_path_within_scope_rejects_traversal_and_prefix_collisions() -> None:
    """`..` と、名前が前方一致するだけの兄弟 directory を許さない。"""

    scope = ("src", "docs/spec.md")

    assert path_within_scope("src/app.py", scope) is True
    assert path_within_scope("src", scope) is True
    assert path_within_scope("src/../etc/passwd", scope) is False
    assert path_within_scope("/src/app.py", scope) is False
    # `src-vendor` は `src` の配下ではない (単純な文字列前方一致だと通ってしまう)。
    assert path_within_scope("src-vendor/app.py", scope) is False
    assert path_within_scope("docs/spec.md.bak", scope) is False


def test_frozen_revision_selection_is_deterministic() -> None:
    """凍結 revision は一意に決まり、決まらない構成は fail closed する。"""

    assert select_frozen_revision(allowed_revisions=["v1.2"], default_revision="HEAD") == "v1.2"
    # 空 allowlist は「revision を絞らない」指定。Integration の既定を使う。
    assert select_frozen_revision(allowed_revisions=[], default_revision="main") == "main"
    assert (
        select_frozen_revision(allowed_revisions=["main", "release"], default_revision="main")
        == "main"
    )
    with pytest.raises(RepositoryClientError) as error:
        select_frozen_revision(allowed_revisions=["main", "release"], default_revision="HEAD")
    assert error.value.code == "invalid_request"
