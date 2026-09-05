"""不変 Result に対する追加式人工 Evaluation の domain 契約を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class EvaluationVerdict(StrEnum):
    """人工評価で選択できる Result の正確性判定。"""

    ACCURATE = "accurate"
    PARTIALLY_ACCURATE = "partially_accurate"
    INACCURATE = "inaccurate"
    UNCERTAIN = "uncertain"


class EvaluationResultNotFoundError(LookupError):
    """Project/Run は存在するが評価可能な Result がないことを表す。"""


class InvalidEvaluationRevisionError(ValueError):
    """Revision の JSON Pointer が不正または Result 内に存在しないことを表す。"""


@dataclass(frozen=True, slots=True)
class EvaluationRevisionProposal:
    """利用者が Result の一 field に提示する修正案。"""

    pointer: str
    suggested_value: Any
    reason: str


@dataclass(frozen=True, slots=True)
class StoredEvaluationRevision:
    """Server が Result から原値を補完した監査可能な修正案。"""

    pointer: str
    original_value: Any
    suggested_value: Any
    reason: str


@dataclass(frozen=True, slots=True)
class CreateEvaluationCommand:
    """Project ownership 済み Result へ人工評価を追加する command。"""

    project_id: UUID
    run_id: UUID
    user_id: UUID
    rating: int
    verdict: EvaluationVerdict
    comment: str
    revisions: tuple[EvaluationRevisionProposal, ...]


@dataclass(frozen=True, slots=True)
class StoredEvaluation:
    """Result を変更せず追加された一件の人工評価。"""

    evaluation_id: UUID
    result_id: UUID
    run_id: UUID
    user_id: UUID
    rating: int
    verdict: EvaluationVerdict
    comment: str
    revisions: tuple[StoredEvaluationRevision, ...]
    created_at: datetime


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 JSON Pointer を Result に解決し、存在する原値だけを返す。"""

    if not pointer or not pointer.startswith("/"):
        raise InvalidEvaluationRevisionError("Revision pointer must start with '/'")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = _decode_pointer_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                raise InvalidEvaluationRevisionError(f"Revision pointer does not exist: {pointer}")
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise InvalidEvaluationRevisionError(f"Revision array index is invalid: {pointer}")
            index = int(token)
            if index >= len(current):
                raise InvalidEvaluationRevisionError(f"Revision pointer does not exist: {pointer}")
            current = current[index]
            continue
        raise InvalidEvaluationRevisionError(f"Revision pointer traverses a scalar: {pointer}")
    return current


def _decode_pointer_token(token: str) -> str:
    """JSON Pointer token の ~0/~1 escape だけを受理して復号する。"""

    decoded: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise InvalidEvaluationRevisionError("Revision pointer contains an invalid escape")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)
