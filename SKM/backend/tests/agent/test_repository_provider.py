"""実 repository.read/v1 Provider の応答・Evidence・失敗境界を検証する (計画 §19 W4)。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from skillmind.agent.domain import RegisteredTool, RunWorkspace
from skillmind.agent.repository_client import (
    RepositoryClientError,
    RepositoryCommit,
    RepositoryListing,
)
from skillmind.agent.repository_provider import RepositoryReadProvider
from skillmind.agent.repository_source import RepositoryBindingRef, ScopedRepositorySession
from skillmind.agent.tool_gateway import RunToolContext, ToolProviderError

_INTEGRATION_ID = uuid4()
_BINDING_ID = uuid4()
_REVISION = "d" * 40


class _StubSession:
    """固定内容を返す RepositorySession の fake。"""

    def __init__(self, payload: bytes) -> None:
        """固定 payload と revision を保持する。"""

        self.provider = "git"
        self.revision = _REVISION
        self._payload = payload

    async def list_files(self, paths: Sequence[str]) -> RepositoryListing:
        """空の listing を返す。"""

        del paths
        return RepositoryListing(entries=(), skipped=())

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """固定 payload を返す。"""

        del path, max_bytes
        return self._payload

    async def read_history(
        self, paths: Sequence[str], *, limit: int
    ) -> tuple[RepositoryCommit, ...]:
        """空の履歴を返す。"""

        del paths, limit
        return ()


class _StubSource:
    """binding 解決済みとして scope 付き session を貸し出す source の fake。"""

    def __init__(self, payload: bytes, *, scope_paths: tuple[str, ...] = ("src",)) -> None:
        """貸し出す payload と scope を固定する。"""

        self._payload = payload
        self._scope_paths = scope_paths
        self.requested_revisions: list[str | None] = []

    @asynccontextmanager
    async def open(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        binding: RepositoryBindingRef,
        requested_revision: str | None = None,
    ) -> AsyncIterator[ScopedRepositorySession]:
        """解決済み binding として stub session を貸し出す。"""

        del project_id, run_id, binding
        self.requested_revisions.append(requested_revision)
        yield ScopedRepositorySession(
            session=_StubSession(self._payload), scope_paths=self._scope_paths
        )


def _context(tmp_path: Path, *, bound: bool = True) -> RunToolContext:
    """repository.read/v1 を解決済みの Run Tool 境界を組む。"""

    root = tmp_path / "run"
    for name in ("", "workspace", "input", "output", "temp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return RunToolContext(
        run_id=uuid4(),
        run_attempt_id=uuid4(),
        project_id=uuid4(),
        user_id=uuid4(),
        tool=RegisteredTool(
            capability="repository.read/v1",
            sdk_name="mcp__skillmind__repository_read_v1",
            provider="git",
            integration_id=_INTEGRATION_ID if bound else None,
            input_schema={"type": "object"},
            binding_id=_BINDING_ID if bound else None,
        ),
        workspace=RunWorkspace(
            root=root,
            cwd=root / "workspace",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        ),
    )


@pytest.mark.asyncio
async def test_reads_a_bound_file_with_reproducible_evidence(tmp_path: Path) -> None:
    """Scope 内 file を読み、解決済み revision と content hash を Evidence に残す。"""

    source = _StubSource(b"def main():\n    return 1\n")

    result = await RepositoryReadProvider(source).execute(
        _context(tmp_path),
        {"revision": "main", "path": "src/app.py", "purpose": "Inspect the entry point"},
    )

    assert result.response["status"] == "success"
    assert result.response["provider"] == "git"
    # Agent が渡した式ではなく、解決済み revision を返す (再現可能な参照になる)。
    assert result.response["revision"] == _REVISION
    assert result.response["content"].startswith("def main():")
    assert result.response["content_hash"].startswith("sha256:")
    assert source.requested_revisions == ["main"]
    evidence = result.evidence[0]
    assert evidence.evidence_type == "source_code"
    assert evidence.source_uri == f"git://integration/{_INTEGRATION_ID}/{_REVISION}/src/app.py"
    assert evidence.source_locator["revision"] == _REVISION


@pytest.mark.asyncio
async def test_out_of_scope_path_is_rejected(tmp_path: Path) -> None:
    """凍結 scope 外の path は Provider error として Agent へ返す。"""

    source = _StubSource(b"secret\n")

    with pytest.raises(ToolProviderError) as error:
        await RepositoryReadProvider(source).execute(
            _context(tmp_path),
            {"revision": "main", "path": "etc/passwd", "purpose": "Escape"},
        )

    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_binary_file_is_reported_as_invalid_request(tmp_path: Path) -> None:
    """非 UTF-8 file は content として返さず、明確な error にする。"""

    source = _StubSource(b"\xff\xfe\x00")

    with pytest.raises(ToolProviderError) as error:
        await RepositoryReadProvider(source).execute(
            _context(tmp_path),
            {"revision": "main", "path": "src/blob.bin", "purpose": "Read a binary"},
        )

    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_unbound_tool_snapshot_is_rejected(tmp_path: Path) -> None:
    """Integration 束縛の無い Run snapshot では実 repository を読ませない。"""

    source = _StubSource(b"data\n")

    with pytest.raises(ToolProviderError) as error:
        await RepositoryReadProvider(source).execute(
            _context(tmp_path, bound=False),
            {"revision": "main", "path": "src/app.py", "purpose": "Read"},
        )

    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_client_failures_are_mapped_to_stable_codes(tmp_path: Path) -> None:
    """Client の失敗 code は凭据を含まないまま Agent へ伝わる。"""

    class _FailingSource(_StubSource):
        """open が常に失敗する source の fake。"""

        @asynccontextmanager
        async def open(
            self,
            *,
            project_id: UUID,
            run_id: UUID,
            binding: RepositoryBindingRef,
            requested_revision: str | None = None,
        ) -> AsyncIterator[ScopedRepositorySession]:
            """安定 code 付きの失敗を送出する。"""

            del project_id, run_id, binding, requested_revision
            raise RepositoryClientError(
                "unavailable", "Repository credential was rejected", retryable=False
            )
            yield  # pragma: no cover - 到達しない (contextmanager 形式の維持のみ)

    with pytest.raises(ToolProviderError) as error:
        await RepositoryReadProvider(_FailingSource(b"")).execute(
            _context(tmp_path),
            {"revision": "main", "path": "src/app.py", "purpose": "Read"},
        )

    assert error.value.code == "unavailable"
    assert error.value.message == "Repository credential was rejected"
