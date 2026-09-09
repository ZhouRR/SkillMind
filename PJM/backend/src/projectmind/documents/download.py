"""文書 metadata を添付応答の安全な HTTP header 値へ変換する。"""

from __future__ import annotations

import re
from urllib.parse import quote

_MEDIA_TYPE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*/[a-zA-Z0-9][a-zA-Z0-9!#$&^_.+-]*")


def attachment_disposition(name: str) -> str:
    """保存名を変更せず、path・制御字を除いた単一名を UTF-8 と ASCII で提供する。"""

    component = name.replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(character if character.isprintable() else "_" for character in component)
    safe = safe.strip(" .")[:200] or "document"
    fallback = re.sub(r"[^a-zA-Z0-9 ._-]", "_", safe).strip(" .") or "document"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(safe, safe='')}"


def attachment_media_type(mime: str) -> str:
    """未検証の metadata で HTTP header を拡張せず、未知値は binary 添付に閉じる。"""

    if len(mime) > 128 or _MEDIA_TYPE.fullmatch(mime) is None:
        return "application/octet-stream"
    return mime.lower()
