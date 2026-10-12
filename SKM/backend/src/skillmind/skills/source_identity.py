"""原 file index と、明示選択した入口から不変 source identity を計算する。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from skillmind.core.hashing import sha256_hex


def skill_source_hash(
    files: Sequence[Mapping[str, Any]], *, entry_path: str | None = None,
) -> str:
    """旧 source は元の配列形式、新しい選択 source は入口も含めた hash を使う。"""

    value: list[dict[str, Any]] | dict[str, Any] = [dict(file) for file in files]
    if entry_path is not None:
        value = {"entry_path": entry_path, "files": value}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"sha256:{sha256_hex(encoded)}"
