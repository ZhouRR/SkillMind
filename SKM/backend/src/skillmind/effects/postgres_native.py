"""原生 SQL 書込を既存の原 Effect 回执 table と同一 transaction で確定する。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from asyncpg import PostgresError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from skillmind.agent.postgres_native import bind_native_parameters, native_statement, query_rows
from skillmind.agent.postgres_source import create_database_engine
from skillmind.agent.tool_sequence import matches_checks
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.domain import (
    ChangeProposalDraft,
    ChangeProposalValidationError,
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.postgres_write import DatabaseWriteConflictError, DatabaseWriteReceipt
from skillmind.effects.redmine import EffectProviderStaleError, EffectProviderTransportError

SQL_WRITE = "database.execute/v1"
SQL_VERSION = "postgres-native/v1"

SQL_OBSERVATION_MESSAGE = (
    "No matching SQL observation was found. Before proposing, call database.query on the same "
    "resource with the exact read_back SELECT, parameters and limit. Use its non-truncated "
    "response file content_hash as precondition.revision and include its evidence_refs. "
    "For INSERT, observe the target row's absence. Schema, UUID and time queries are not target "
    "observations. No write was submitted; correct this proposal."
)
SQL_PRECONDITION_MESSAGE = (
    "The target query result changed before SQL execution; no write was sent. Read the same "
    "read_back SELECT with the same parameters and limit again, reassess the intended change, "
    "and submit a corrected proposal using that observation. Do not retry the unchanged proposal."
)


class SqlObservationValidationError(ChangeProposalValidationError):
    """提案作成前に、別 query の hash や未確認の Evidence を修正可能として拒否する。"""


class SqlPreconditionNotMetError(EffectProviderStaleError):
    """初回 DML 未送信と transaction の終了を確認した前提不一致だけを表す。"""


class _SqlObservationChanged(Exception):
    """読取段階の不一致を rollback 完了後まで外へ公開しない内部 signal。"""


def sql_payload(
    operation: str,
    changes: tuple[dict[str, Any], ...],
    scope: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """SQL/parameters/read-back をそのまま凍結する。SQL を model に再生成させない。"""
    if config.get("access_mode") != "native_sql" or len(changes) != 1:
        raise ValueError("Native SQL connection and one change are required")
    change = changes[0]
    value = change.get("value")
    if (
        change.get("path") != "/statement"
        or change.get("action") != "SET"
        or not isinstance(value, dict)
        or set(value) != {"statement", "read_back", "checks"}
    ):
        raise ValueError("SQL requires SET /statement with statement, read_back and checks")
    _, _, actual = native_statement(value["statement"], scope, write=True)
    native_statement(value["read_back"], scope, write=False)
    if (
        operation != actual
        or not isinstance(value["checks"], list)
        or not 1 <= len(value["checks"]) <= 50
    ):
        raise ValueError("SQL operation or read-back checks are invalid")
    for check in value["checks"]:
        if (
            not isinstance(check, dict)
            or set(check) != {"pointer", "equals"}
            or not isinstance(check["pointer"], str)
            or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", check["pointer"]) is None
        ):
            raise ValueError("SQL read-back requires exact JSON pointer checks")
    if len(canonical_json(value).encode("utf-8")) > 1_048_576:
        raise ValueError("SQL proposal exceeds the existing effect size limit")
    return dict(value)


def validate_sql_proposal(
    draft: ChangeProposalDraft, scope: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """権限は DB account、操作種別と原観測は platform が確認する。"""
    try:
        value = sql_payload(draft.operation, draft.changes, scope, config)
        if (
            draft.capability_version != SQL_WRITE
            or draft.target.get("locator") != "database"
            or re.fullmatch(r"sha256:[a-f0-9]{64}", str(draft.precondition.get("revision", "")))
            is None
        ):
            raise ValueError("SQL target must be database with the original query file hash")
        return {**value, "operation": draft.operation}
    except (ValueError, TypeError, KeyError) as error:
        raise ChangeProposalValidationError("Native SQL proposal is invalid") from error


def sql_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """回読 SELECT と実際の DML を明示する。"""
    return {"statements": ["SELECT", payload["operation"]]}


class NativeDatabaseWriteProvider:
    """commit 不明時は原 receipt の照会だけを許し、未検出でも再実行しない。"""

    def __init__(self, authorize: Callable[[ClaimedEffectExecution, str], Awaitable[Any]]) -> None:
        """既存 Effect supervisor の現在権限を各段階で確認する。"""
        self._authorize = authorize

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """任意 SQL 文を複数 transaction に分割せず、read-back と receipt を原子的に保存する。"""
        if (
            not credential
            or execution.capability_version != SQL_WRITE
            or execution.provider != "postgres"
        ):
            raise EffectProviderTransportError("credential_unavailable", retryable=False)
        payload = sql_payload(
            execution.operation,
            execution.changes,
            execution.integration_scope,
            execution.integration_config,
        )
        checksum = native_request_checksum(
            execution.run_id, execution.integration_id, payload, execution.precondition
        )
        engine = create_database_engine(execution.integration_config, credential, read_only=False)
        replayed = False
        try:
            async with asyncio.timeout(30), engine.connect() as connection, connection.begin():
                await connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                await connection.exec_driver_sql("SET LOCAL statement_timeout = '15000'")
                await connection.exec_driver_sql("SET LOCAL lock_timeout = '2000'")
                await self._authorize(execution, credential)
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {
                        "key": int.from_bytes(
                            execution.effect_execution_id.bytes[:8], "big", signed=True
                        )
                    },
                )
                previous = (
                    (
                        await connection.execute(
                            text(
                                "SELECT request_checksum, "
                                "CASE WHEN octet_length(before_row::text) <= 1048576 "
                                "THEN before_row::text END AS before_row, "
                                "CASE WHEN octet_length(after_row::text) <= 1048576 "
                                "THEN after_row::text END AS after_row "
                                "FROM skillmind_effects.execution_receipts WHERE effect_id = :id"
                            ),
                            {"id": execution.effect_execution_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if previous is not None:
                    if previous["request_checksum"] != checksum:
                        raise EffectProviderStaleError("Original SQL receipt differs")
                    try:
                        before, after = (
                            json.loads(previous["before_row"]),
                            json.loads(previous["after_row"]),
                        )
                    except (ValueError, TypeError):
                        raise EffectProviderStaleError("Original SQL receipt is invalid") from None
                    if (
                        not isinstance(before, dict)
                        or not isinstance(after, dict)
                        or before.get("truncated") is not False
                        or after.get("truncated") is not False
                        or "sha256:" + sha256_hex(canonical_json(before))
                        != execution.precondition["revision"]
                        or not matches_checks(after, payload["checks"])
                    ):
                        raise EffectProviderStaleError("Original SQL receipt does not match")
                    replayed = True
                else:
                    if execution.attempt_no != 1:
                        raise EffectProviderTransportError(
                            "sql_original_result_unconfirmed", retryable=False
                        )
                    before = await query_rows(
                        connection, payload["read_back"], execution.integration_scope
                    )
                    if (
                        before["truncated"]
                        or len(canonical_json(before).encode("utf-8")) > 1_048_576
                        or "sha256:" + sha256_hex(canonical_json(before))
                        != execution.precondition["revision"]
                    ):
                        raise _SqlObservationChanged()
                    sql, parameters, _ = native_statement(
                        payload["statement"], execution.integration_scope, write=True
                    )
                    raw_connection = await connection.get_raw_connection()
                    driver = raw_connection.driver_connection
                    if driver is None:
                        raise ValueError("PostgreSQL driver is unavailable")
                    parameters = await bind_native_parameters(driver, sql, parameters)
                    await self._authorize(execution, credential)
                    await driver.execute(sql, *parameters)
                    after = await query_rows(
                        connection, payload["read_back"], execution.integration_scope
                    )
                    if (
                        after["truncated"]
                        or len(canonical_json(after).encode("utf-8")) > 1_048_576
                        or not matches_checks(after, payload["checks"])
                    ):
                        raise EffectProviderStaleError(
                            "SQL read-back differs; transaction rolled back"
                        )
                    if credential in canonical_json([before, after]):
                        raise EffectProviderStaleError(
                            "SQL receipt contains connection credentials"
                        )
                    await connection.execute(
                        text(
                            "INSERT INTO skillmind_effects.execution_receipts "
                            "(effect_id, request_checksum, before_row, after_row) "
                            "VALUES (:id, :checksum, CAST(:before AS jsonb), CAST(:after AS jsonb))"
                        ),
                        {
                            "id": execution.effect_execution_id,
                            "checksum": checksum,
                            "before": canonical_json(before),
                            "after": canonical_json(after),
                        },
                    )
                if credential in canonical_json([before, after]):
                    raise EffectProviderStaleError("SQL receipt contains connection credentials")
                await self._authorize(execution, credential)
        except _SqlObservationChanged:
            # with の rollback が失敗した場合はこの分岐に来ない。旧回执の不一致や
            # DML/commit 後の障害を「未送信」に変えず、撤権も先に確認する。
            await self._authorize(execution, credential)
            raise SqlPreconditionNotMetError(SQL_PRECONDITION_MESSAGE) from None
        except (SQLAlchemyError, PostgresError, OSError, TimeoutError):
            raise EffectProviderTransportError("sql_result_unconfirmed", retryable=False) from None
        finally:
            await engine.dispose()
        return EffectProviderResult(
            before=_evidence(execution, before, "before"),
            after=_evidence(execution, after, "after"),
            replayed=replayed,
            verification={
                "method": "READ_BACK",
                "matched_paths": ["/statement"],
                "replayed": replayed,
            },
        )


def _evidence(
    execution: ClaimedEffectExecution, value: dict[str, Any], phase: str
) -> EffectEvidenceDraft:
    """原 transaction の JSON 観測を Effect と結び付ける。"""
    return EffectEvidenceDraft(
        evidence_type="resource",
        source_uri=f"postgres-query://{execution.integration_id}/{execution.effect_execution_id}/{phase}",
        source_locator={"effect_id": str(execution.effect_execution_id), "phase": phase},
        content=value,
        excerpt=None,
        metadata={"meaning": "Original SQL transaction receipt"},
    )


@dataclass(frozen=True, slots=True)
class NativeSqlReceiptCommand:
    """SQL を再実行する権限を持たない原 transaction の識別と確認条件。"""

    effect_id: UUID
    project_id: UUID
    run_id: UUID
    integration_id: UUID
    request_checksum: str
    expected_revision: str
    checks_json: str = field(repr=False)


def native_request_checksum(
    run_id: UUID,
    integration_id: UUID | None,
    payload: Mapping[str, Any],
    precondition: Mapping[str, Any],
) -> str:
    """apply と只読照会で原 SQL/parameter の同一 identity を使用する。"""
    return "sha256:" + sha256_hex(
        canonical_json(
            [
                str(run_id),
                str(integration_id),
                {key: payload[key] for key in ("statement", "read_back", "checks")},
                precondition,
            ]
        )
    )


def validate_native_receipt(
    command: NativeSqlReceiptCommand, receipt: DatabaseWriteReceipt
) -> None:
    """現在の業務行でなく、原 read-back の事前 hash と確認条件を照合する。"""
    if (
        not isinstance(receipt.before, dict)
        or not isinstance(receipt.after, dict)
        or receipt.before.get("truncated") is not False
        or receipt.after.get("truncated") is not False
        or "sha256:" + sha256_hex(canonical_json(receipt.before)) != command.expected_revision
        or not matches_checks(receipt.after, json.loads(command.checks_json))
    ):
        raise DatabaseWriteConflictError("Original SQL receipt does not match")


async def lookup_native_receipt(
    config: Mapping[str, Any],
    credential: str,
    command: NativeSqlReceiptCommand,
    *,
    authorize: Callable[[], Awaitable[None]],
) -> DatabaseWriteReceipt | None:
    """結果未知の照会では READ ONLY で元回执だけ読み、DML/再 claim を行わない。"""
    engine = create_database_engine(config, credential, read_only=True)
    try:
        async with asyncio.timeout(30), engine.connect() as connection, connection.begin():
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            await connection.exec_driver_sql("SET LOCAL statement_timeout = '15000'")
            await authorize()
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT request_checksum, CASE WHEN octet_length(before_row::text) <= 1048576 "
                            "THEN before_row::text END AS before_row, "
                            "CASE WHEN octet_length(after_row::text) <= 1048576 THEN after_row::text END AS after_row "
                            "FROM skillmind_effects.execution_receipts WHERE effect_id = :id"
                        ),
                        {"id": command.effect_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            await authorize()
            if row is None:
                return None
            if row["request_checksum"] != command.request_checksum:
                raise DatabaseWriteConflictError("Original SQL request differs")
            try:
                receipt = DatabaseWriteReceipt(
                    json.loads(row["before_row"]), json.loads(row["after_row"]), True
                )
            except (ValueError, TypeError):
                raise DatabaseWriteConflictError("Original SQL receipt is invalid") from None
            validate_native_receipt(command, receipt)
            if credential in canonical_json([receipt.before, receipt.after]):
                raise DatabaseWriteConflictError("Original SQL receipt contains credentials")
            return receipt
    except (SQLAlchemyError, PostgresError, OSError, TimeoutError):
        raise EffectProviderTransportError(
            "sql_original_result_unconfirmed", retryable=False
        ) from None
    finally:
        await engine.dispose()
