"""安全 I/O の早期打切りと、失敗時に既存データを守る境界を検証する。"""

from __future__ import annotations

import os
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from projectmind.agent import materialization_storage as storage


def test_concurrent_generation_creation_has_one_namespace_winner(tmp_path: Path) -> None:
    """別 UUID の競争でも同じ Run に二つの候補を作らない。"""

    identities = (uuid4(), uuid4())

    def prepare(snapshot_id: UUID) -> UUID | None:
        """独占作成に勝った識別子だけを返す。"""

        try:
            storage.create_generation(tmp_path, snapshot_id)
        except storage.MaterializationError:
            return None
        return snapshot_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [identity for identity in executor.map(prepare, identities) if identity]
    assert len(winners) == 1
    namespace = tmp_path / ".projectmind-inputs"
    assert list(namespace.iterdir()) == [namespace / str(winners[0])]


def test_failed_generation_creation_preserves_namespace_and_cannot_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """namespace 作成後の I/O 失敗でも痕跡を消さず、別 UUID で再開しない。"""

    identity = uuid4()
    original = storage.os.mkdir

    def fail_generation(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        """namespace の同期後に子 directory の作成だけを失敗させる。"""

        if path == str(identity):
            raise OSError("generation creation failed")
        original(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(storage.os, "mkdir", fail_generation)
    with pytest.raises(storage.MaterializationError):
        storage.create_generation(tmp_path, identity)
    namespace = tmp_path / ".projectmind-inputs"
    assert namespace.is_dir() and list(namespace.iterdir()) == []
    monkeypatch.setattr(storage.os, "mkdir", original)
    with pytest.raises(storage.MaterializationError):
        storage.create_generation(tmp_path, uuid4())
    assert list(namespace.iterdir()) == []


@pytest.mark.parametrize("limit", ["files", "entries"])
def test_bounded_discovery_closes_lazy_iterator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    """上限到達で全件を列挙せず、開いた directory iterator を直ちに閉じる。"""

    reference = tmp_path / "file"
    reference.write_bytes(b"x")
    info = reference.stat()
    seen: list[int] = []
    closed: list[bool] = []

    def entries(
        root: Path, relative: str | None
    ) -> Generator[tuple[str, os.stat_result], None, None]:
        """途中までしか消費されない iterator の cleanup を可視化する。"""

        try:
            for index in range(1000):
                seen.append(index)
                yield str(index), info
        finally:
            closed.append(True)

    monkeypatch.setattr(storage, "_tree_entries", entries)
    files, truncated = storage.list_workspace_files(
        tmp_path,
        "workspace",
        max_files=2 if limit == "files" else 100,
        max_entries=2 if limit == "entries" else 100,
    )
    assert files == ("0", "1") and truncated
    assert seen == [0, 1, 2] and closed == [True]


def test_duplicate_directory_is_rejected_and_iterator_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rename 競争等で同じ directory が再出現しても検証を無限に続けない。"""

    info = tmp_path.stat()
    closed: list[bool] = []

    def entries(
        root: Path, relative: str | None
    ) -> Generator[tuple[str, os.stat_result], None, None]:
        """同じ許可 directory を重複報告し、拒否時の資源解放を検査する。"""

        try:
            yield "documents", info
            yield "documents", info
            raise AssertionError("Duplicate directory was not rejected")
        finally:
            closed.append(True)

    monkeypatch.setattr(storage, "_tree_entries", entries)
    with pytest.raises(storage.MaterializationError, match="unexpected directory"):
        storage.verify_tree(tmp_path, expected_files={"documents/file.md"})
    assert closed == [True]


def test_replace_failure_preserves_existing_workspace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書込完成前の失敗で既存 inode を切り詰めず、自分の一時 file だけを回収する。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "report.md"
    original.write_bytes(b"original")

    def fail_replace(*args: object, **kwargs: object) -> None:
        """directory entry の置換直前で同期障害を注入する。"""

        raise OSError("replace failed")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(storage.MaterializationError):
        storage.write_workspace_file(tmp_path, "workspace/report.md", b"replacement")
    assert original.read_bytes() == b"original"
    assert list(workspace.iterdir()) == [original]
