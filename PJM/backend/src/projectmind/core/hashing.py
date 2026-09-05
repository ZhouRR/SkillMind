"""正規化 JSON と SHA-256 の共通実装を一箇所に固定する。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Key 順と区切り文字を固定し、NaN を拒否した deterministic JSON を返す。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_hex(value: str | bytes) -> str:
    """UTF-8 正規化した入力の SHA-256 hex digest を返す。"""

    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()
