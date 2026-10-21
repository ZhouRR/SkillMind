"""承認済み原生 SQL を一度実行し、業務上の観測・判定は Agent に委ねる。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from asyncpg import PostgresError
from sqlalchemy.exc import SQLAlchemyError

from skillmind.agent.database_errors import sqlstate_of
from skillmind.agent.postgres_native import bind_native_parameters, native_statement
from skillmind.agent.postgres_source import create_database_engine
from skillmind.core.hashing import canonical_json
from skillmind.effects.domain import (
    ChangeProposalDraft,
    ChangeProposalValidationError,
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.redmine import EffectProviderTransportError

SQL_WRITE = "database.execute/v1"
SQL_VERSION = "postgres-native/v2"
SQL_FAILURE_MESSAGES = {
    "sql_request_not_sent": (
        "The database request was not sent. Check the connection and retry with a new proposal"
        " if appropriate."
    ),
    "sql_undefined_column": (
        "The SQL references an unknown column. Inspect the table schema and correct the "
        "statement."
    ),
    "sql_undefined_table": "The SQL references an unknown table. Verify its schema and table name.",
    "sql_permission_denied": "The database account denied this operation. Check its permissions.",
    "sql_invalid_parameter": (
        "The SQL parameter or expression type is invalid. Check the actual column types and "
        "correct the statement."
    ),
    "sql_constraint_violation": (
        "The SQL violates a database constraint. Inspect the relevant data and table "
        "constraints."
    ),
    "sql_syntax_error": "The database rejected the SQL syntax. Correct the statement.",
    "sql_transaction_rolled_back": (
        "The SQL transaction was rolled back. Inspect the relevant schema and data before "
        "deciding the next operation."
    ),
}


class SqlExecutionError(EffectProviderTransportError):
    """未送信、または rollback 完了を確認した SQL の固定診断。"""

    def __init__(self, code: str) -> None:
        """内部分類だけを保持し、driver の SQL・値・接続情報を公開しない。"""
        if code not in SQL_FAILURE_MESSAGES:
            raise ValueError("Invalid SQL failure code")
        super().__init__(code, retryable=False)


def sql_failure_message(code: str) -> str:
    """Agent に返す固定診断と原 transaction の確定事実を組み立てる。"""
    message = SQL_FAILURE_MESSAGES.get(code, "")
    if message and code != "sql_request_not_sent":
        message += " The transaction was rolled back; no changes were committed."
    return message


def _rollback_code(error: BaseException) -> str:
    """SQLSTATE で分類し、サーバーの詳細本文や hint は反射しない。"""
    state = sqlstate_of(error)
    if state == "42703":
        return "sql_undefined_column"
    if state == "42P01":
        return "sql_undefined_table"
    if state == "42501":
        return "sql_permission_denied"
    if state and (state.startswith("22") or state in {"42804", "42883"}):
        return "sql_invalid_parameter"
    if state and state.startswith("23"):
        return "sql_constraint_violation"
    if state == "42601":
        return "sql_syntax_error"
    return "sql_transaction_rolled_back"


def sql_payload(
    operation: str,
    changes: tuple[dict[str, Any], ...],
    scope: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """一つの DML と引数を承認に固定し、業務用 SELECT は要求しない。"""
    if (
        config.get("access_mode") != "native_sql"
        or len(changes) != 1
        or changes[0].get("path") != "/statement"
        or changes[0].get("action") != "SET"
    ):
        raise ValueError("SQL requires one SET /statement")
    value = changes[0].get("value")
    if (
        not isinstance(value, dict)
        or set(value) != {"statement"}
        or not isinstance(value["statement"], dict)
        or set(value["statement"]) - {"sql", "parameters"}
    ):
        raise ValueError("SQL requires statement with sql and optional parameters")
    _, _, command = native_statement(value["statement"], scope, write=True)
    if command != operation:
        raise ValueError("SQL operation differs from the approved statement")
    if len(canonical_json(value).encode("utf-8")) > 1_048_576:
        raise ValueError("SQL proposal exceeds the existing effect size limit")
    return dict(value)


def validate_sql_proposal(
    draft: ChangeProposalDraft,
    scope: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """原 SQL と現在資源権限を検査し、外部回執表や事前観測に依存しない。"""
    try:
        if (
            draft.capability_version != SQL_WRITE
            or draft.target.get("locator") != "database"
            or draft.precondition
            or draft.verification.get("method") != "SQL_RESPONSE"
        ):
            raise ValueError("SQL writes use SQL_RESPONSE without a platform precondition")
        return {
            **sql_payload(draft.operation, draft.changes, scope, config),
            "operation": draft.operation,
        }
    except ValueError as error:
        raise ChangeProposalValidationError(str(error)) from None


def sql_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """承認済み DML に必要な statement 権限だけを返す。"""
    return {"statements": [payload["operation"]]}


class NativeDatabaseWriteProvider:
    """一回の transaction 結果を返し、結果不明の再 claim は再送しない。"""

    def __init__(self, authorize: Callable[[ClaimedEffectExecution], Awaitable[None]]) -> None:
        """現在の批准・資源・lease を検査する共有認可を保持する。"""
        self._authorize = authorize

    async def apply(
        self,
        execution: ClaimedEffectExecution,
        *,
        credential: str | None,
    ) -> EffectProviderResult:
        """原 DML を commit してから応答し、rollback 確認と commit 不明を区別する。"""
        if (
            credential is None
            or execution.capability_version != SQL_WRITE
            or execution.provider != "postgres"
        ):
            raise ValueError("SQL write requires a PostgreSQL credential")
        payload = sql_payload(
            execution.operation,
            execution.changes,
            execution.integration_scope,
            execution.integration_config,
        )
        await self._authorize(execution)
        if execution.attempt_no != 1:
            raise EffectProviderTransportError("sql_result_unconfirmed", retryable=False)
        if credential in canonical_json(payload):
            raise ValueError("SQL request contains credentials")
        engine = create_database_engine(execution.integration_config, credential, read_only=False)
        sent = False
        statement_error: BaseException | None = None
        try:
            async with (
                asyncio.timeout(30), engine.connect() as connection, connection.begin(),
            ):
                try:
                    await connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
                    await connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
                    sql, parameters, _ = native_statement(
                        payload["statement"],
                        execution.integration_scope,
                        write=True,
                    )
                    raw = await connection.get_raw_connection()
                    driver = raw.driver_connection
                    if driver is None:
                        raise ValueError("PostgreSQL driver is unavailable")
                    parameters = await bind_native_parameters(driver, sql, parameters)
                    await self._authorize(execution)
                    sent = True
                    command_tag = await driver.execute(sql, *parameters)
                    await self._authorize(execution)
                except (
                    SQLAlchemyError,
                    PostgresError,
                    OSError,
                    TimeoutError,
                    ValueError,
                ) as error:
                    if isinstance(error, PermissionError):
                        raise
                    statement_error = error
                    raise
            # begin の正常終了が commit 応答の確認点。行の値から成功を推測しない。
        except (SQLAlchemyError, PostgresError, OSError, TimeoutError, ValueError) as error:
            if isinstance(error, PermissionError):
                raise
            await self._authorize(execution)
            if statement_error is not None and error is statement_error:
                # 同じ例外が transaction を抜けた場合だけ rollback 完了を確認できる。
                raise SqlExecutionError(_rollback_code(statement_error)) from None
            if not sent:
                raise SqlExecutionError("sql_request_not_sent") from None
            raise EffectProviderTransportError("sql_result_unconfirmed", retryable=False) from None
        finally:
            await engine.dispose()
        await self._authorize(execution)
        result = {"command_tag": command_tag, "affected_rows": int(command_tag.rsplit(" ", 1)[1])}
        return EffectProviderResult(
            before=_evidence(execution, payload, "request"),
            after=_evidence(execution, result, "response"),
            replayed=False,
            verification={
                "method": "SQL_RESPONSE",
                "matched_paths": ["/statement"],
                "committed": True,
                "business_verdict": "NOT_EVALUATED",
            },
        )


def _evidence(
    execution: ClaimedEffectExecution,
    value: dict[str, Any],
    phase: str,
) -> EffectEvidenceDraft:
    """要求と応答を監査保存し、変更前後の業務データと混同しない。"""
    return EffectEvidenceDraft(
        evidence_type="resource",
        source_uri=f"postgres-query://{execution.integration_id}/{execution.effect_execution_id}/{phase}",
        source_locator={"effect_id": str(execution.effect_execution_id), "phase": phase},
        content=value,
        excerpt=None,
        metadata={
            "meaning": "Approved SQL request"
            if phase == "request"
            else "Committed SQL command response"
        },
    )
