"""Run 文書集合から実 input までの閉じた境界と retry cache を検証する。"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from projectmind.agent import workspace_materializer as module
from projectmind.agent.materialization_storage import read_file, write_new_file
from projectmind.agent.workspace import WorkspaceManager
from projectmind.agent.workspace_materializer import MaterializationError, WorkspaceMaterializer
from projectmind.documents.source import ProjectDocumentContent
from tests.agent.test_binary_text import _workbook
from tests.agent.test_workspace_materializer import _DOC_BLUEPRINT, _FakeInventory
from tests.documents.fakes import document_content, document_snapshot


def _run(
    tmp_path: Path,
    *,
    available: Sequence[ProjectDocumentContent],
    selected: Sequence[ProjectDocumentContent],
) -> tuple[_FakeInventory, WorkspaceMaterializer, dict[str, Any]]:
    """取得可能集合と明示選択を別々に受け、実行前の凍結引数を作る。"""

    project_id, run_id = uuid4(), uuid4()
    inventory = _FakeInventory(available)
    materializer = WorkspaceMaterializer(
        document_inventory=inventory, max_bytes=10_485_760, max_files=500
    )
    return (
        inventory,
        materializer,
        {
            "workspace": WorkspaceManager(tmp_path / "runs").initialize(run_id),
            "project_id": project_id,
            "run_id": run_id,
            "blueprint": _DOC_BLUEPRINT,
            "document_snapshots": (document_snapshot(project_id, selected),),
        },
    )


def _manifest(arguments: dict[str, Any]) -> Path:
    """fixture Run の document manifest の実 path を返す。"""

    return arguments["workspace"].input_dir / "documents/.projectmind/manifest.json"


async def test_materialization_uses_selected_subset_not_project_inventory(tmp_path: Path) -> None:
    """Project に別文書が存在しても明示した一件以外は input に現れない。"""

    selected = document_content(name="selected.md")
    hidden = document_content(name="not-selected.md")
    inventory, materializer, arguments = _run(
        tmp_path, available=[selected, hidden], selected=[selected]
    )
    result = await materializer.materialize(**arguments)
    assert inventory.calls == 1
    assert result[0].files == 1
    assert not (arguments["workspace"].input_dir / "documents/specs/not-selected.md").exists()


@pytest.mark.parametrize("required", [True, False])
async def test_missing_selection_does_not_authorize_all(tmp_path: Path, required: bool) -> None:
    """必須未選択は拒否、任意未選択は無読取となり、どちらも全集へ退避しない。"""

    content = document_content()
    inventory, materializer, arguments = _run(tmp_path, available=[content], selected=[content])
    arguments["document_snapshots"] = ()
    arguments["blueprint"] = {
        "resource_requirements": [{"key": "config", "kind": "document", "required": required}]
    }
    if required:
        with pytest.raises(MaterializationError):
            await materializer.materialize(**arguments)
    else:
        assert await materializer.materialize(**arguments) == ()
    assert inventory.calls == 0
    assert not list(arguments["workspace"].input_dir.iterdir())


async def test_multiple_slots_keep_membership_and_materialize_union_once(tmp_path: Path) -> None:
    """slot 間の交差を重複物化せず、所属と選択 mode は manifest に残す。"""

    first, second = document_content(name="one.md"), document_content(name="two.md")
    inventory, materializer, arguments = _run(tmp_path, available=[first, second], selected=[first])
    one = replace(arguments["document_snapshots"][0], selection_mode="SINGLE")
    two = replace(
        document_snapshot(arguments["project_id"], [first, second], key="reference"),
        selection_mode="SET",
    )
    arguments["document_snapshots"] = (one, two)
    arguments["blueprint"] = {
        "resource_requirements": [
            {"key": key, "kind": "document", "required": True} for key in ("config", "reference")
        ]
    }
    result = await materializer.materialize(**arguments)
    manifest = json.loads(_manifest(arguments).read_text())
    assert inventory.calls == 1 and result[0].files == 2
    assert manifest["scope"]["requirements"] == {
        "config": one.to_json(),
        "reference": two.to_json(),
    }


@pytest.mark.parametrize("problem", ["missing", "different_id", "changed_bytes"])
async def test_invalid_inventory_fails_before_creating_tree(tmp_path: Path, problem: str) -> None:
    """凍結後の削除・同名再登録・虚偽 hash を skip に変換しない。"""

    content = document_content()
    available = {
        "missing": [],
        "different_id": [replace(content, document_id=uuid4())],
        "changed_bytes": [replace(content, data=b"changed")],
    }[problem]
    _, materializer, arguments = _run(tmp_path, available=available, selected=[content])
    with pytest.raises(MaterializationError):
        await materializer.materialize(**arguments)
    assert not list(arguments["workspace"].input_dir.iterdir())


@pytest.mark.parametrize("converted", [False, True])
async def test_valid_cache_survives_source_deletion(tmp_path: Path, converted: bool) -> None:
    """検証済みの input は現在の文書庫が空でも同じ descriptor で再利用できる。"""

    content = document_content()
    if converted:
        content = document_content(_workbook([("Design", '<row r="1"/>')]), name="design.xlsx")
    inventory, materializer, arguments = _run(tmp_path, available=[content], selected=[content])
    first = await materializer.materialize(**arguments)
    empty_inventory = _FakeInventory([])
    retry = WorkspaceMaterializer(
        document_inventory=empty_inventory, max_bytes=10_485_760, max_files=500
    )
    assert await retry.materialize(**arguments) == first
    assert inventory.calls == 1 and empty_inventory.calls == 0


@pytest.mark.parametrize("identity", ["project_id", "run_id", "selection"])
async def test_cache_cannot_be_rebound_to_another_run_selection(
    tmp_path: Path, identity: str
) -> None:
    """他 Project/Run/選択から同じ input path を再利用しても、identity 検査で閉じる。"""

    content = document_content()
    inventory, materializer, arguments = _run(tmp_path, available=[content], selected=[content])
    await materializer.materialize(**arguments)
    if identity == "selection":
        arguments["document_snapshots"] = (
            document_snapshot(arguments["project_id"], [document_content(name="new.md")]),
        )
    else:
        arguments[identity] = uuid4()
        if identity == "project_id":
            arguments["document_snapshots"] = (
                document_snapshot(arguments["project_id"], [content]),
            )
    with pytest.raises(MaterializationError):
        await materializer.materialize(**arguments)
    assert inventory.calls == 1


@pytest.mark.parametrize(
    "tamper", ["extra_file", "empty_directory", "symlink", "hardlink", "fifo", "missing"]
)
async def test_cache_rejects_unexpected_or_unsafe_files(tmp_path: Path, tamper: str) -> None:
    """manifest 外の内容や特殊 file を受け入れず、静かな修復も行わない。"""

    content = document_content()
    inventory, materializer, arguments = _run(tmp_path, available=[content], selected=[content])
    await materializer.materialize(**arguments)
    root = arguments["workspace"].input_dir / "documents"
    target = root / "specs/overview.md"
    if tamper == "extra_file":
        (root / "extra.md").write_text("unexpected")
    elif tamper == "empty_directory":
        (root / "empty").mkdir()
    else:
        target.unlink()
        outside = tmp_path / "outside.md"
        outside.write_bytes(content.data)
        if tamper == "symlink":
            target.symlink_to(outside)
        elif tamper == "hardlink":
            os.link(outside, target)
        elif tamper == "fifo":
            os.mkfifo(target)
    with pytest.raises(MaterializationError):
        await materializer.materialize(**arguments)
    assert inventory.calls == 1


async def test_manifest_cannot_smuggle_an_unselected_path_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cache の path を改ざんしても、凍結集合の外側を開く前に拒否する。"""

    content = document_content()
    _, materializer, arguments = _run(tmp_path, available=[content], selected=[content])
    await materializer.materialize(**arguments)
    manifest_path = _manifest(arguments)
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["path"] = "documents/../../../outside.md"
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest))
    reads: list[str] = []

    def recorded_read(root: Path, path: str, *, max_bytes: int) -> bytes:
        """実 I/O を保ったまま、開いた path を監査する。"""

        reads.append(path)
        return read_file(root, path, max_bytes=max_bytes)

    monkeypatch.setattr(module, "read_file", recorded_read)
    with pytest.raises(MaterializationError):
        await materializer.materialize(**arguments)
    assert reads == ["documents/.projectmind/manifest.json"]


async def test_partial_cache_is_preserved_and_never_rebuilt(tmp_path: Path) -> None:
    """manifest のない途中 tree は新規物化で上書きせず、調査可能なまま残す。"""

    content = document_content()
    inventory, materializer, arguments = _run(tmp_path, available=[content], selected=[content])
    root = arguments["workspace"].input_dir / "documents"
    root.mkdir()
    marker = root / "partial.md"
    marker.write_text("keep for investigation")
    with pytest.raises(MaterializationError):
        await materializer.materialize(**arguments)
    assert marker.read_text() == "keep for investigation"
    assert inventory.calls == 0


def test_storage_never_overwrites_existing_target_or_follows_parent_link(tmp_path: Path) -> None:
    """O_EXCL と directory descriptor の境界で既存内容や root 外を守る。"""

    target = tmp_path / "kept.md"
    target.write_bytes(b"original")
    with pytest.raises(MaterializationError):
        write_new_file(tmp_path, "kept.md", b"replacement")
    assert target.read_bytes() == b"original"
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "input"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MaterializationError):
        write_new_file(root, "linked/created.md", b"must not escape")
    assert not list(outside.iterdir())
