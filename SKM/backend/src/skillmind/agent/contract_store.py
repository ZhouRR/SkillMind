"""Configured root 内の versioned JSON 契約を読み込む。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ContractStore:
    """Configured contracts root 下の JSON object だけを読み込む。"""

    def __init__(self, root: Path) -> None:
        """Existence を検証し、contract path 解決の root を固定する。"""

        self._root = root.resolve(strict=True)

    @property
    def root(self) -> Path:
        """Tool 契約を解決する read-only root を返す。"""

        return self._root

    def load(self, relative_path: str) -> dict[str, Any]:
        """Traversal と non-object JSON を拒否し、契約の defensive copy を返す。"""

        path = (self._root / relative_path).resolve(strict=True)
        if not path.is_relative_to(self._root) or path.is_symlink():
            raise ValueError("Contract path escapes the configured root")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Contract JSON must be an object")
        return value
