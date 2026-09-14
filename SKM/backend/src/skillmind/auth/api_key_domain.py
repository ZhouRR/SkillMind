"""API key の公開 metadata と、一回だけ返す秘密を分離する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class ApiKeyNotFoundError(LookupError):
    """不存在と他組織の key を区別せず拒否する。"""


@dataclass(frozen=True, slots=True)
class StoredApiKey:
    """一覧に公開可能な発行・利用・失効情報。hash/proof は含めない。"""

    id: UUID
    name: str
    key_prefix: str
    created_by: UUID
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreatedApiKey:
    """発行 response だけが持つ原 key。repr や一覧へ混ぜない。"""

    record: StoredApiKey
    token: str = field(repr=False)
