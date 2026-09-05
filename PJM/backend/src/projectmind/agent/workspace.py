"""Run 単位の受け渡し用 workspace を安全に初期化する。"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from projectmind.agent.domain import RunWorkspace


class WorkspaceManager:
    """Worker 専用 root 下に Run ごとの固定 directory 構成を作成する。"""

    def __init__(self, root: Path) -> None:
        """Relative root を拒否し、作成先の security boundary を固定する。"""

        if not root.is_absolute():
            raise ValueError("Workspace root must be absolute")
        if root.is_symlink():
            raise ValueError("Workspace root must not be a symbolic link")
        self._root = root.absolute()

    def initialize(self, run_id: UUID) -> RunWorkspace:
        """Symlink を許容せず、retry 可能な Run workspace を決定的に作成する。"""

        self._ensure_directory(self._root)
        run_root = self._root / str(run_id)
        paths = (
            run_root,
            run_root / "workspace",
            run_root / "input",
            run_root / "output",
            run_root / "temp",
        )
        for path in paths:
            self._ensure_directory(path)
        return RunWorkspace(
            root=run_root,
            cwd=paths[1],
            input_dir=paths[2],
            output_dir=paths[3],
            temp_dir=paths[4],
        )

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        """Existing symlink/file による workspace 逃逸を directory 作成前に防ぐ。"""

        if path.is_symlink():
            raise ValueError("Workspace path must not be a symbolic link")
        if path.exists() and not path.is_dir():
            raise ValueError("Workspace path must be a directory")
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
