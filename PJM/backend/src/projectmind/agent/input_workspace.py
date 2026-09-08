"""実行に渡された input 世代を、独立回执と実 byte の双方で確認する。"""

from __future__ import annotations

from projectmind.agent.domain import RunWorkspace
from projectmind.agent.materialization_storage import (
    MaterializationError,
    read_sealed_file,
    relative_parts,
    verify_input_files,
    verify_tree,
)


def input_relative(workspace: RunWorkspace) -> str:
    """resolve で symlink を正規化せず、Run root からの検証済み経路を返す。"""

    try:
        relative = workspace.input_dir.relative_to(workspace.root).as_posix()
    except ValueError as error:
        raise MaterializationError("Input generation is outside the Run workspace") from error
    relative_parts(relative)
    return relative


def verify_input(workspace: RunWorkspace, *, content: bool = True) -> None:
    """回执未取得と合法な空 input を区別し、未知・欠損の tree を渡さない。"""

    if workspace.input_files is None:
        raise MaterializationError("Run input has no trusted receipt")
    relative = input_relative(workspace)
    if content:
        verify_input_files(workspace.root, relative=relative, files=workspace.input_files)
    else:
        verify_tree(
            workspace.root, relative=relative, expected_files=set(workspace.input_file_index)
        )


def read_input_file(workspace: RunWorkspace, path: str, *, max_bytes: int) -> bytes:
    """回执の一 file だけを安全に開き、返す byte 自体の同一性を証明する。"""

    if workspace.input_files is None or path not in workspace.input_file_index:
        raise MaterializationError("Input file is not present in the trusted receipt")
    return read_sealed_file(
        workspace.root,
        path=f"{input_relative(workspace)}/{path}",
        seal=workspace.input_file_index[path],
        max_bytes=max_bytes,
    )
