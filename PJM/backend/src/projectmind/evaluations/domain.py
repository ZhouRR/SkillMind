"""不変 Result に対する追加式人工 Evaluation の domain 契約を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any
from uuid import UUID

from projectmind.core.hashing import canonical_json, sha256_hex


class EvaluationVerdict(StrEnum):
    """人工評価で選択できる Result の正確性判定。"""

    ACCURATE = "accurate"
    PARTIALLY_ACCURATE = "partially_accurate"
    INACCURATE = "inaccurate"
    UNCERTAIN = "uncertain"


class EvaluationError(Exception):
    """資格の最終確認を必要とする Evaluation の領域拒否。"""


class EvaluationResultNotFoundError(EvaluationError, LookupError):
    """Project/Run は存在するが評価可能な Result がないことを表す。"""


class InvalidEvaluationRevisionError(EvaluationError, ValueError):
    """Revision の JSON Pointer が不正または Result 内に存在しないことを表す。"""


class InvalidEvaluationCommandError(EvaluationError, ValueError):
    """非 JSON 値や不正 identity を含む要求を保存前に拒否する。"""


class EvaluationResultMismatchError(EvaluationError):
    """要求が表示していた Result と原 Run の Result が一致しない。"""


class EvaluationSubmissionConflictError(EvaluationError):
    """原キーに対応する保存済み内容と新しい要求内容が異なる。"""


class EvaluationSubmissionNotFoundError(EvaluationError, LookupError):
    """同一 actor と Result の原要求が現在の読取で確認できない。"""


class InvalidEvaluationCursorError(EvaluationError, ValueError):
    """同じ Result に属するページ位置として確認できない。"""


class EvaluationIntegrityError(EvaluationError):
    """保存済み評価の損傷を原要求の成功として扱わない。"""


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


@dataclass(frozen=True, slots=True)
class StoredEvaluationSubmission:
    """commit 後に返す原回执。重放フラグは HTTP status 選択専用で公開しない。"""

    project_id: UUID
    run_id: UUID
    submission_key: UUID
    evaluation: StoredEvaluation
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class StoredEvaluationPage:
    """同じ Result の有界履歴ページ。総件数や跨頁 snapshot を表さない。"""

    project_id: UUID
    run_id: UUID
    result_id: UUID
    items: tuple[StoredEvaluation, ...]
    next_cursor: UUID | None


def require_evaluation_uuid(value: UUID) -> None:
    """公開 UUID の型と非空 identity を内部 caller にも要求する。"""

    if not isinstance(value, UUID) or value.int == 0:
        raise InvalidEvaluationCommandError("Evaluation identity must be a non-nil UUID")


def validate_evaluation_command(command: CreateEvaluationCommand) -> None:
    """HTTP に依存せず長さ・厳密な型・JSON 値と UTF-8 を検証する。"""

    for identity in (command.project_id, command.run_id, command.user_id):
        require_evaluation_uuid(identity)
    if type(command.rating) is not int or not 1 <= command.rating <= 5:
        raise InvalidEvaluationCommandError("Evaluation rating must be between 1 and 5")
    if not isinstance(command.verdict, EvaluationVerdict):
        raise InvalidEvaluationCommandError("Evaluation verdict is invalid")
    if type(command.comment) is not str or len(command.comment) > 4000:
        raise InvalidEvaluationCommandError("Evaluation comment is invalid")
    if type(command.revisions) is not tuple or len(command.revisions) > 100:
        raise InvalidEvaluationCommandError("Evaluation revisions are invalid")
    pointers: set[str] = set()
    values: list[Any] = [command.comment]
    for revision in command.revisions:
        if not isinstance(revision, EvaluationRevisionProposal):
            raise InvalidEvaluationCommandError("Evaluation revision is invalid")
        if (
            type(revision.pointer) is not str
            or not 1 <= len(revision.pointer) <= 512
            or not revision.pointer.startswith("/")
        ):
            raise InvalidEvaluationRevisionError("Revision pointer is invalid")
        if revision.pointer in pointers:
            raise InvalidEvaluationRevisionError("Revision pointers must be unique")
        pointers.add(revision.pointer)
        if type(revision.reason) is not str or not 1 <= len(revision.reason) <= 1000:
            raise InvalidEvaluationCommandError("Evaluation revision reason is invalid")
        values.extend((revision.pointer, revision.reason, revision.suggested_value))
    strict_evaluation_json(values)


def strict_evaluation_json(value: Any) -> str:
    """tuple や非文字 key を JSON へ暗黙変換せず、共有 canonical 表現を返す。"""

    def require_json(item: Any) -> None:
        """JSON の厳密な型を再帰検査し、循環・過深は固定拒否へ畳み込む。"""

        if item is None or type(item) in (bool, int):
            return
        if type(item) is float and isfinite(item):
            return
        if type(item) is str:
            item.encode("utf-8")
            return
        if type(item) is list:
            for child in item:
                require_json(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for key, child in item.items():
                require_json(key)
                require_json(child)
            return
        raise InvalidEvaluationCommandError("Evaluation requires finite UTF-8 JSON values")

    try:
        require_json(value)
        return canonical_json(value)
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidEvaluationCommandError(
            "Evaluation requires finite UTF-8 JSON values",
        ) from error


def evaluation_request_hash(
    command: CreateEvaluationCommand,
    *,
    result_id: UUID,
    submission_key: UUID,
) -> str:
    """原利用者・Result と有序修訂を結び、bool と number を同一視しない。"""

    validate_evaluation_command(command)
    require_evaluation_uuid(result_id)
    require_evaluation_uuid(submission_key)
    return "sha256:" + sha256_hex(
        strict_evaluation_json(
            {
                "version": "projectmind.evaluation-submission/v1",
                "submission_key": str(submission_key),
                "project_id": str(command.project_id),
                "run_id": str(command.run_id),
                "result_id": str(result_id),
                "user_id": str(command.user_id),
                "rating": command.rating,
                "verdict": command.verdict.value,
                "comment": command.comment,
                "revisions": [
                    {
                        "pointer": item.pointer,
                        "suggested_value": item.suggested_value,
                        "reason": item.reason,
                    }
                    for item in command.revisions
                ],
            }
        )
    )


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
            if (
                not token.isascii()
                or not token.isdigit()
                or (len(token) > 1 and token.startswith("0"))
            ):
                raise InvalidEvaluationRevisionError(f"Revision array index is invalid: {pointer}")
            if len(token) > len(str(len(current))):
                raise InvalidEvaluationRevisionError("Revision array index is invalid")
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
