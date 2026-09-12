"""PostgreSQL の一行変更を固定 identity・明示列・原状態へ束縛する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from skillmind.agent.postgres_source import MAX_DATABASE_BYTES, identifier
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError

DATABASE_WRITE_CAPABILITY = "database.write/v1"
DATABASE_WRITE_PROVIDER_VERSION = "postgres-receipt/v1"


@dataclass(frozen=True, slots=True)
class DatabaseWriteCommand:
    """モデル可変 object を JSON と checksum に凍結した、まだ権限を持たない変更。"""

    effect_id: UUID
    project_id: UUID
    run_id: UUID
    integration_id: UUID
    table: str
    operation: Literal["INSERT", "UPDATE"]
    key_json: str
    values_json: str
    expected_json: str
    checksum: str

    @property
    def key(self) -> dict[str, Any]:
        """呼出ごとに新しい主キー object を返す。"""

        value: dict[str, Any] = json.loads(self.key_json)
        return value

    @property
    def values(self) -> dict[str, Any]:
        """呼出ごとに新しい変更列 object を返す。"""

        value: dict[str, Any] = json.loads(self.values_json)
        return value

    @property
    def expected(self) -> dict[str, Any] | None:
        """承認した原行全体、または INSERT の不在条件を返す。"""

        value: dict[str, Any] | None = json.loads(self.expected_json)
        return value


def build_database_write(
    *,
    effect_id: UUID,
    project_id: UUID,
    run_id: UUID,
    integration_id: UUID,
    table: str,
    operation: str,
    key: Mapping[str, Any],
    values: Mapping[str, Any],
    expected: Mapping[str, Any] | None,
    scope: Mapping[str, Any],
) -> DatabaseWriteCommand:
    """表・列・操作を frozen scope と照合し、任意 SQL/主キー変更/暗黙上書きを拒否する。"""

    if any(
        not isinstance(item, UUID) or item.int == 0
        for item in (effect_id, project_id, run_id, integration_id)
    ):
        raise ValueError("Database effect identity is invalid")
    payload = database_write_payload(
        table=table, operation=operation, key=key, values=values, expected=expected, scope=scope
    )
    body = {
        "effect_id": str(effect_id),
        "project_id": str(project_id),
        "run_id": str(run_id),
        "integration_id": str(integration_id),
        **payload,
        "provider_version": DATABASE_WRITE_PROVIDER_VERSION,
    }
    encoded = canonical_json(body)
    if len(encoded.encode("utf-8")) > MAX_DATABASE_BYTES:
        raise ValueError("Database effect exceeds the byte limit")
    return DatabaseWriteCommand(
        effect_id,
        project_id,
        run_id,
        integration_id,
        table,
        "INSERT" if operation == "INSERT" else "UPDATE",
        canonical_json(body["key"]),
        canonical_json(body["values"]),
        canonical_json(body["expected"]),
        f"sha256:{sha256_hex(encoded)}",
    )


def database_write_payload(
    *,
    table: str,
    operation: str,
    key: Mapping[str, Any],
    values: Mapping[str, Any],
    expected: Mapping[str, Any] | None,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """提案と実行で同じ単行・列範囲規則を適用し、呼出元から独立した値を返す。"""

    if not isinstance(table, str) or len(table.split(".")) != 2:
        raise ValueError("Database target requires schema.table")
    for part in table.split("."):
        identifier(part)
    if table.startswith("skillmind_effects."):
        raise ValueError("The effect receipt schema is reserved")
    if operation not in {"INSERT", "UPDATE"} or operation not in scope.get("operations", []):
        raise ValueError("Database operation is outside the frozen scope")
    if not isinstance(scope.get("operations"), list) or not isinstance(scope.get("tables"), list):
        raise ValueError("Database scope requires explicit lists")
    allowed_columns = scope.get("write_columns", [])
    if table not in scope.get("tables", []) or not isinstance(allowed_columns, list):
        raise ValueError("Database table is outside the frozen scope")
    columns = [
        value.removeprefix(table + ".")
        for value in allowed_columns
        if isinstance(value, str) and value.startswith(table + ".")
    ]
    if not columns:
        raise ValueError("Database write columns must be explicit")
    for column in columns:
        identifier(column)
    if not isinstance(key, Mapping) or not 1 <= len(key) <= 20:
        raise ValueError("Database write requires an exact primary key")
    if not isinstance(values, Mapping) or not 1 <= len(values) <= 100:
        raise ValueError("Database write requires explicit values")
    for column in (*key, *values):
        identifier(column)
    if set(key).intersection(values):
        raise ValueError("Database primary key cannot be changed")
    written_columns = set(values) | (set(key) if operation == "INSERT" else set())
    if not written_columns.issubset(columns):
        raise ValueError("Database write columns exceed the frozen scope")
    if any(value is None or type(value) not in {str, int, bool} for value in key.values()):
        raise ValueError("Database primary key values are invalid")
    if operation == "INSERT" and expected is not None:
        raise ValueError("Database insert requires an absent row")
    if operation == "UPDATE" and (not isinstance(expected, Mapping) or not expected):
        raise ValueError("Database update requires the observed original row")
    if expected is not None and any(
        column not in expected or canonical_json(expected[column]) != canonical_json(value)
        for column, value in key.items()
    ):
        raise ValueError("Database original row does not match its primary key")
    payload = {
        "table": table,
        "operation": operation,
        "key": dict(key),
        "values": dict(values),
        "expected": dict(expected) if expected is not None else None,
    }
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > MAX_DATABASE_BYTES:
        raise ValueError("Database effect exceeds the byte limit")
    frozen: dict[str, Any] = json.loads(encoded)
    return frozen


def database_row_revision(row: Mapping[str, Any] | None) -> str:
    """観測行全体の revision と、INSERT の不在条件を同じ規則で表す。"""

    return "absent" if row is None else f"sha256:{sha256_hex(canonical_json(dict(row)))}"


def database_proposal_payload(
    *,
    operation: str,
    target: Mapping[str, Any],
    changes: tuple[dict[str, Any], ...],
    precondition: Mapping[str, Any],
    verification: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """汎用 change.propose の一つの /row SET を固定された DB 変更へ変換する。"""

    if len(changes) != 1 or set(changes[0]) != {"path", "action", "value"}:
        raise ValueError("Database proposal requires exactly one row change")
    change = changes[0]
    row = change["value"]
    if (
        change["path"] != "/row"
        or change["action"] != "SET"
        or not isinstance(row, Mapping)
        or set(row) != {"key", "values", "expected"}
        or verification.get("method") != "READ_BACK"
        or verification.get("paths") != ["/row"]
    ):
        raise ValueError("Database proposal row or verification is invalid")
    table = target.get("locator")
    if not isinstance(table, str):
        raise ValueError("Database target requires schema.table")
    payload = database_write_payload(
        table=table,
        operation=operation,
        key=row["key"],
        values=row["values"],
        expected=row["expected"],
        scope=scope,
    )
    if dict(precondition) != {"revision": database_row_revision(payload["expected"])}:
        raise ValueError("Database proposal revision differs from the observed row")
    return payload


def validate_database_write_proposal(
    draft: ChangeProposalDraft, *, binding_scope: Mapping[str, Any]
) -> dict[str, Any]:
    """候補を単行契約へ制限する。観測 Evidence の来歴と権限は repository が検証する。"""

    if draft.capability_version != DATABASE_WRITE_CAPABILITY:
        raise ChangeProposalValidationError("Effect capability is not database.write/v1")
    try:
        return database_proposal_payload(
            operation=draft.operation,
            target=draft.target,
            changes=draft.changes,
            precondition=draft.precondition,
            verification=draft.verification,
            scope=binding_scope,
        )
    except (ValueError, TypeError) as error:
        raise ChangeProposalValidationError(
            "Database proposal is outside the row contract"
        ) from error


def database_write_scope_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """単行変更が使う最小の表/列/操作範囲を返す。事前許可は別途禁止される。"""

    columns = set(payload["values"])
    if payload["operation"] == "INSERT":
        columns.update(payload["key"])
    return {
        "tables": [payload["table"]],
        "operations": [payload["operation"]],
        "write_columns": [f"{payload['table']}.{column}" for column in sorted(columns)],
    }


def database_observation_matches(
    locator: Mapping[str, Any], payload: Mapping[str, Any], *, integration_id: UUID
) -> bool:
    """信頼済み read Evidence の完全主キー条件と原行/不在の観測を照合する。"""

    expected = payload["expected"]
    return (
        locator.get("integration_id") == str(integration_id)
        and locator.get("table") == payload["table"]
        and locator.get("offset") == 0
        and locator.get("truncated") is False
        and locator.get("columns") == []
        and canonical_json(locator.get("filters")) == canonical_json(payload["key"])
        and locator.get("row_count") == (0 if expected is None else 1)
        and locator.get("row_hashes")
        == ([] if expected is None else [database_row_revision(expected)])
    )
