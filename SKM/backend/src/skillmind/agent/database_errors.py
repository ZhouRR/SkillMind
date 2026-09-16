"""SQLSTATE と失敗段階だけから診断し、SQL・値・driver 本文は外部へ渡さない。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DatabaseStage = Literal["connect", "schema_read", "row_read", "result_decode", "cleanup"]


@dataclass(frozen=True)
class DatabaseDiagnostic:
    """永続監査に使用できる固定分類と公開応答。"""

    reason_code: str
    stage: DatabaseStage
    sqlstate: str | None
    code: str
    message: str


class DatabaseReadError(RuntimeError):
    """元例外の本文を保存しない、段階付きの読取失敗。"""

    def __init__(
        self,
        message: str = "Database read could not be completed",
        *,
        diagnostic: DatabaseDiagnostic | None = None,
    ) -> None:
        """旧注入 Source と互換にしつつ、未分類の診断本文は常に固定する。"""
        self.diagnostic = diagnostic or classify_database_error(None, "row_read")
        super().__init__(self.diagnostic.message)


def sqlstate_of(error: BaseException | None) -> str | None:
    """SQLAlchemy orig と driver cause を有界に辿り、構造化 SQLSTATE のみ読む。"""
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 12:
        item = pending.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        for key in ("sqlstate", "pgcode"):
            value = getattr(item, key, None)
            if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{5}", value):
                return value
        for key in ("orig", "__cause__", "__context__"):
            value = getattr(item, key, None)
            if isinstance(value, BaseException):
                pending.append(value)
    return None


def classify_database_error(
    error: BaseException | None, stage: DatabaseStage, *, target_lookup: bool = False
) -> DatabaseDiagnostic:
    """取消・不明な障害を、修正可能な caller query と誤認しない。"""
    state = sqlstate_of(error)
    reason, code = "read_failed", "unavailable"
    message = "Database read could not be completed; this does not establish a connection outage."
    if stage == "row_read" and state == "42703":
        reason, code = "undefined_column", "invalid_request"
        message = (
            "The query references a column that does not exist. Inspect the same authorized "
            "table's schema and correct this read once if the Skill permits recovery. "
            "Do not treat this as a connection outage or repeat the unchanged query."
        )
    elif stage == "row_read" and state in {"42804", "42883", "22P02", "22007", "22008", "22003"}:
        reason, code = "parameter_type_mismatch", "invalid_request"
        message = (
            "The query parameter or operator does not match the column type. Inspect the "
            "same authorized table's schema and correct this read once if the Skill permits. "
            "Do not drop filters or guess another field."
        )
    elif state == "42P01" and (stage == "row_read" or target_lookup):
        reason, code = "undefined_table", "not_found"
        message = (
            "The authorized target table was not found. Verify its exact name; do not "
            "guess a replacement."
        )
    elif state == "42501":
        reason, code = "insufficient_privilege", "scope_denied"
        message = (
            "The database denied access. Stop this access; do not bypass permissions or "
            "change fields to evade it."
        )
    elif (state and state.startswith("08")) or (
        stage == "connect" and isinstance(error, (OSError, TimeoutError))
    ):
        reason = "connection_failure"
        message = (
            "The database connection failed. Follow the Skill's environment failure "
            "policy; do not change query fields."
        )
    return DatabaseDiagnostic(reason, stage, state, code, message)


def safe_database_diagnostic(value: object) -> dict[str, str | None] | None:
    """固定分類のみを永続化し、外部 Provider の任意本文を診断欄へ通さない。"""
    if not isinstance(value, dict):
        return None
    reasons = {
        "undefined_column",
        "parameter_type_mismatch",
        "undefined_table",
        "insufficient_privilege",
        "connection_failure",
        "read_failed",
    }
    stages = {"connect", "schema_read", "row_read", "result_decode", "cleanup"}
    state = value.get("sqlstate")
    if (
        not isinstance(value.get("reason_code"), str)
        or not isinstance(value.get("stage"), str)
        or value["reason_code"] not in reasons
        or value["stage"] not in stages
    ):
        return None
    if state is not None and (
        not isinstance(state, str) or not re.fullmatch(r"[0-9A-Z]{5}", state)
    ):
        return None
    return {"reason_code": value["reason_code"], "stage": value["stage"], "sqlstate": state}
