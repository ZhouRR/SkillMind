"""永続 occurrence の原設定を、現在の定義や資源から独立して厳格に復元する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.runs.creation_request import TaskRunIntent
from projectmind.schedules.cron import normalize_timezone, parse_cron
from projectmind.schedules.domain import (
    ScheduleDefinition,
    ScheduleKind,
    ScheduleOccurrenceConflictError,
    schedule_idempotency_key,
)

_FIELDS = frozenset(
    {
        "snapshot_version",
        "schedule_id",
        "occurrence_at",
        "idempotency_key",
        "name",
        "configuration_version",
        "claim_row_version",
        "definition",
        "request",
        "exhausted",
        "missed",
    }
)
_DEFINITION_FIELDS = frozenset(
    {
        "kind",
        "timezone",
        "cron_expression",
        "run_at",
        "end_at",
        "max_runs",
    }
)


def utc_time(value: datetime, *, minute: bool = False) -> datetime:
    """暗黙の host timezone や丸めによる発火 identity の変更を拒否する。"""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Schedule time must include a timezone")
    result = value.astimezone(UTC)
    if minute and (result.second != 0 or result.microsecond != 0):
        raise ValueError("Occurrence time must be an exact minute")
    return result


def positive_integer(value: object, *, name: str, maximum: int = 2_147_483_647) -> int:
    """bool や float を整数世代・上限として受け入れない。"""

    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be a positive bounded integer")
    return value


def _stamp(value: datetime | None) -> str | None:
    """UTC の単一表記を checksum の一部として固定する。"""

    return utc_time(value).isoformat().replace("+00:00", "Z") if value is not None else None


def _time(value: object, *, minute: bool = False) -> datetime:
    """保存済み時刻は canonical UTC 表記だけを復元する。"""

    if not isinstance(value, str):
        raise ValueError("Stored schedule time is invalid")
    result = utc_time(datetime.fromisoformat(value.replace("Z", "+00:00")), minute=minute)
    if _stamp(result) != value:
        raise ValueError("Stored schedule time is not canonical")
    return result


def definition_json(definition: ScheduleDefinition) -> dict[str, Any]:
    """時計の現在値に依存せず、元の発火規則を保存する。"""

    return {
        "kind": definition.kind.value,
        "timezone": definition.timezone,
        "cron_expression": definition.cron_expression,
        "run_at": _stamp(definition.run_at),
        "end_at": _stamp(definition.end_at),
        "max_runs": definition.max_runs,
    }


def _definition(value: object) -> ScheduleDefinition:
    """過去の ONCE を現在時刻で再検証せず、保存形式と種類の整合性を確認する。"""

    if not isinstance(value, dict) or set(value) != _DEFINITION_FIELDS:
        raise ValueError("Stored schedule definition is invalid")
    kind = ScheduleKind(value["kind"])
    timezone = normalize_timezone(value["timezone"])
    cron = value["cron_expression"]
    run_at = _time(value["run_at"], minute=True) if value["run_at"] is not None else None
    end_at = _time(value["end_at"]) if value["end_at"] is not None else None
    maximum = value["max_runs"]
    if maximum is not None:
        positive_integer(maximum, name="max_runs")
    if kind is ScheduleKind.CRON:
        if not isinstance(cron, str) or run_at is not None or parse_cron(cron).expression != cron:
            raise ValueError("Stored cron definition is invalid")
    elif cron is not None or run_at is None:
        raise ValueError("Stored one-time definition is invalid")
    return ScheduleDefinition(kind, timezone, cron, run_at, end_at, maximum)


@dataclass(frozen=True, slots=True)
class ScheduleOccurrenceSnapshot:
    """新しい Run hash を作らず、既存 TaskRunIntent と発火規則を封じる。"""

    schedule_id: UUID
    occurrence_at: datetime
    name: str
    configuration_version: int
    claim_row_version: int
    definition: ScheduleDefinition
    intent: TaskRunIntent
    exhausted: bool
    missed: int

    @property
    def idempotency_key(self) -> str:
        """既存の schedule key 規則を保持する。"""

        return schedule_idempotency_key(
            schedule_id=self.schedule_id, occurrence_at=self.occurrence_at
        )

    def to_json(self) -> dict[str, Any]:
        """可変の入力辞書を共有しない JSON 投影を返す。"""

        return {
            "snapshot_version": "v1",
            "schedule_id": str(self.schedule_id),
            "occurrence_at": _stamp(self.occurrence_at),
            "idempotency_key": self.idempotency_key,
            "name": self.name,
            "configuration_version": self.configuration_version,
            "claim_row_version": self.claim_row_version,
            "definition": definition_json(self.definition),
            "request": self.intent.to_json(),
            "exhausted": self.exhausted,
            "missed": self.missed,
        }

    @property
    def checksum(self) -> str:
        """原要求と計画を単一の共通 canonical JSON 実装で結び付ける。"""

        return sha256_hex(canonical_json(self.to_json()))

    @classmethod
    def from_json(cls, value: object, *, checksum: str | None = None) -> ScheduleOccurrenceSnapshot:
        """未知版・欠落・余分な field・数値型の破損は補造せず拒否する。"""

        try:
            if (
                not isinstance(value, dict)
                or set(value) != _FIELDS
                or value["snapshot_version"] != "v1"
            ):
                raise ValueError("Unsupported occurrence snapshot")
            if (
                type(value["exhausted"]) is not bool
                or type(value["missed"]) is not int
                or not 0 <= value["missed"] <= 2_147_483_647
            ):
                raise ValueError("Stored occurrence plan is invalid")
            if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 200:
                raise ValueError("Stored schedule name is invalid")
            result = cls(
                schedule_id=UUID(value["schedule_id"]),
                occurrence_at=_time(value["occurrence_at"], minute=True),
                name=value["name"],
                configuration_version=positive_integer(
                    value["configuration_version"], name="configuration_version"
                ),
                claim_row_version=positive_integer(
                    value["claim_row_version"], name="claim_row_version"
                ),
                definition=_definition(value["definition"]),
                intent=TaskRunIntent.from_json(value["request"]),
                exhausted=value["exhausted"],
                missed=value["missed"],
            )
            encoded = canonical_json(value)
            if encoded != canonical_json(result.to_json()):
                raise ValueError("Occurrence snapshot is not canonical")
            if checksum is not None and sha256_hex(encoded) != checksum:
                raise ValueError("Occurrence snapshot checksum does not match")
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ScheduleOccurrenceConflictError(
                "Stored occurrence snapshot is invalid"
            ) from error
