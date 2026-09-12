"""非同期解釈の原要求と、永続化しない開始資格を定義する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


class InterpretationRequestNotFoundError(RuntimeError):
    """不存在と組織外の原要求を同じ応答にする。"""


class InterpretationRequestConflictError(RuntimeError):
    """同じ原要求 ID へ別の入力・actor・会話を割り当てない。"""

    def __init__(self, message: str, *, existing_request_id: UUID | None = None) -> None:
        """組織内の同内容だけに、読み取りで確認できる既存 ID を添える。"""

        super().__init__(message)
        self.existing_request_id = existing_request_id


class InterpretationRequestDeniedError(RuntimeError):
    """開始資格の欠落や原会話の失効により Worker の続行を拒否する。"""


@dataclass(frozen=True, slots=True)
class InterpretationRequestSnapshot:
    """Worker 専用の凍結入力。公開応答は本文と会話参照を投影しない。"""

    request_id: UUID
    organization_id: UUID
    actor_id: UUID
    auth_session_id: UUID
    skill_source_id: UUID
    execution_key: str
    input_checksum: str
    input: dict[str, Any] = field(repr=False)
    status: str
    created_at: datetime
    interpretation_id: UUID | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class InterpretationRequestOwner:
    """認領 commit の成功を観測した元 Worker だけが保持する秘密値。"""

    request: InterpretationRequestSnapshot
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class InterpretationCallPermit:
    """一度の呼出し許可。再取得や Queue への保存で開始資格を再発行しない。"""

    owner: InterpretationRequestOwner = field(repr=False)
    call_id: UUID
    ordinal: int
