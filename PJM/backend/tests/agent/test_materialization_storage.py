"""安全 I/O の早期打切りと、失敗時に既存データを守る境界を検証する。"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

from projectmind.agent import materialization_storage as storage


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
