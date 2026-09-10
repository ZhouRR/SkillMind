"""Run 共通予算の内部契約。SDK の生 usage と正規化済み計量を混同しない。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from skillmind.core.hashing import canonical_json, sha256_hex

MAX_UNITS = 2**63 - 1
USD_SCALE = 1_000_000_000


class BudgetError(RuntimeError):
    """不確かな額をゼロへ置き換えず、新規消費を止める予算境界の失敗。"""


class BudgetConflictError(BudgetError):
    """同じ実行/報告鍵へ異なる内容が渡されたことを示す。"""


class BudgetUnavailableError(BudgetError):
    """未導入・未知版・要核対の勘定から新しい予算を発行させない。"""


class BudgetExhaustedError(BudgetError):
    """主子の未消費承諾を含めると、要求全体の額を確保できない。"""


class BudgetStartUncertainError(BudgetUnavailableError):
    """起動意図の commit 成否を確認できず、再起動を許可できない。"""


class MeteringMode(StrEnum):
    """一実行に固定する正規化報告の加算方法。途中で方式を切り替えない。"""

    INCREMENTAL = "INCREMENTAL"
    CUMULATIVE = "CUMULATIVE"


class BudgetExecutionStatus(StrEnum):
    """起動意図を実 process の開始/停止回执と読み替えないための内部状態。"""

    RESERVED = "RESERVED"
    START_INTENT = "START_INTENT"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


def budget_key(value: str) -> str:
    """監査鍵へ任意の本文や制御文字を混入させない。"""

    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value) is None
    ):
        raise BudgetError("Invalid budget identity")
    return value


def budget_start_owner_hash(token: str) -> str:
    """原調整者だけが持つ乱数を専用 hash にし、実行記述子の hash を変更しない。"""

    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise BudgetError("Invalid budget start owner")
    return "sha256:" + sha256_hex(
        canonical_json({"version": "skillmind.budget-start-owner/v1", "token": token})
    )


def budget_units(value: int, *, positive: bool = False) -> int:
    """bool、負数、DB 整数を超える値を計量として受理しない。"""

    if type(value) is not int or not (int(positive) <= value <= MAX_UNITS):
        raise BudgetError("Budget units must be bounded nonnegative integers")
    return value


def usd_to_nanos(value: str | Decimal) -> int:
    """USD を丸めず整数 nano-USD へ変換し、float と過剰精度を拒否する。"""

    if not isinstance(value, str | Decimal):
        raise BudgetError("USD requires an exact decimal representation")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise BudgetError("USD must be finite and nonnegative")
        # Decimal の既定精度で乗算すると巨大入力が丸められるため tuple から整数化する。
        sign, digits, exponent = amount.as_tuple()
        assert isinstance(exponent, int)
        if len(digits) > 40 or abs(exponent) > 40:
            raise BudgetError("USD representation exceeds supported precision")
        coefficient = int("".join(map(str, digits))) * (-1 if sign else 1)
        shift = exponent + 9
        if shift >= 0:
            units = coefficient * 10**shift
        else:
            units, remainder = divmod(coefficient, 10**-shift)
            if remainder:
                raise BudgetError("USD has sub-nano precision")
        return budget_units(units)
    except InvalidOperation as error:
        raise BudgetError("Invalid USD decimal") from error


def budget_checksum(value: Any) -> str:
    """既存 canonical JSON/hash と同じ意味で予算の不変部分を照合する。"""

    return sha256_hex(canonical_json(value))


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Run 作成時に固定する限額と、検証済み adapter 計量 profile の identity。"""

    max_turns: int
    max_cost_nanos: int | None
    source: str
    metering_version: str
    mode: MeteringMode

    def __post_init__(self) -> None:
        """未計量の cost を 0 とせず、無効な policy を導入時に拒否する。"""

        budget_units(self.max_turns, positive=True)
        if self.max_cost_nanos is not None:
            budget_units(self.max_cost_nanos, positive=True)
        budget_key(self.source)
        budget_key(self.metering_version)
        if not isinstance(self.mode, MeteringMode):
            raise BudgetError("Unknown metering mode")

    def to_json(self) -> dict[str, Any]:
        """内部保存形だけを定義し、Run/Event の公開 field を先行追加しない。"""

        return {
            "version": "run-budget/v1",
            "max_turns": self.max_turns,
            "max_cost_nanos": self.max_cost_nanos,
            "source": self.source,
            "metering_version": self.metering_version,
            "mode": self.mode.value,
            "usd_scale": USD_SCALE,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> BudgetPolicy:
        """未知 field/版は旧限額への fallback で吸収しない。"""

        expected = {
            "version",
            "max_turns",
            "max_cost_nanos",
            "source",
            "metering_version",
            "mode",
            "usd_scale",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value["version"] != "run-budget/v1"
            or value["usd_scale"] != USD_SCALE
        ):
            raise BudgetUnavailableError("Unsupported Run budget policy")
        try:
            return cls(
                value["max_turns"],
                value["max_cost_nanos"],
                value["source"],
                value["metering_version"],
                MeteringMode(value["mode"]),
            )
        except (ValueError, TypeError) as error:
            raise BudgetUnavailableError("Invalid stored Run budget policy") from error


@dataclass(frozen=True, slots=True)
class BudgetReservationRequest:
    """SDK Session ID が出る前に決まる一回の有界実行。group は元 Tool 操作を表す。"""

    execution_key: str
    turns: int
    cost_nanos: int | None
    parent_execution_key: str | None = None

    def __post_init__(self) -> None:
        """親子の同一鍵やゼロ額の無料実行を作らせない。"""

        budget_key(self.execution_key)
        budget_units(self.turns, positive=True)
        if self.cost_nanos is not None:
            budget_units(self.cost_nanos, positive=True)
        if self.parent_execution_key is not None:
            budget_key(self.parent_execution_key)
            if self.parent_execution_key == self.execution_key:
                raise BudgetError("Budget execution cannot parent itself")

    def to_json(self) -> dict[str, Any]:
        """要求の再送が実行鍵・親・授与量の全てを比較するようにする。"""

        return {
            "execution_key": self.execution_key,
            "turns": self.turns,
            "cost_nanos": self.cost_nanos,
            "parent_execution_key": self.parent_execution_key,
        }


@dataclass(frozen=True, slots=True)
class BudgetUsageReport:
    """信頼した adapter が一実行分へ正規化した値。rate limit やモデル自称値は対象外。"""

    report_key: str
    source: str
    metering_version: str
    mode: MeteringMode
    turns: int | None
    cost_nanos: int | None
    watermark: int | None = None
    final: bool = False

    def __post_init__(self) -> None:
        """欠測は None のまま保持し、累積系列には独立した水位を必須とする。"""

        for key in (self.report_key, self.source, self.metering_version):
            budget_key(key)
        if not isinstance(self.mode, MeteringMode) or type(self.final) is not bool:
            raise BudgetError("Invalid metering report flags")
        for value in (self.turns, self.cost_nanos):
            if value is not None:
                budget_units(value)
        if self.turns is None and self.cost_nanos is None:
            raise BudgetError("Missing usage is not a zero report")
        if self.mode is MeteringMode.CUMULATIVE:
            if self.watermark is None:
                raise BudgetError("Cumulative usage requires a watermark")
            budget_units(self.watermark)
        elif self.watermark is not None:
            raise BudgetError("Incremental usage cannot claim a cumulative watermark")

    def to_json(self) -> dict[str, Any]:
        """去重時には欠測と final、水位も比較し、同じ鍵の意味を変えない。"""

        return {
            "source": self.source,
            "metering_version": self.metering_version,
            "mode": self.mode.value,
            "turns": self.turns,
            "cost_nanos": self.cost_nanos,
            "watermark": self.watermark,
            "final": self.final,
        }


@dataclass(frozen=True, slots=True)
class BudgetReconciliationClaim:
    """RunAttempt の実行権とは別の、原預留だけを核対する短期実行権。"""

    project_id: UUID
    run_id: UUID
    reservation_id: UUID
    worker_id: str
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class BudgetInvocationBinding:
    """保存した実行の照合値だけを返す。形が正しくても起動権限ではない。"""

    reservation_id: UUID
    invocation_id: UUID
    invocation_checksum: str

    def __post_init__(self) -> None:
        """B の照合で曖昧な識別子や非 canonical checksum を受け付けない。"""

        if (
            not isinstance(self.reservation_id, UUID)
            or not isinstance(self.invocation_id, UUID)
            or not isinstance(self.invocation_checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.invocation_checksum) is None
        ):
            raise BudgetError("Invalid budget invocation binding")


@dataclass(frozen=True, slots=True)
class BudgetExecutionRecord:
    """起動/結算の永続事実を返す。START_INTENT の読戻しは再起動許可ではない。"""

    reservation_id: UUID
    execution_key: str
    status: BudgetExecutionStatus
    granted_turns: int
    reserved_turns: int
    consumed_turns: int
    granted_cost_nanos: int | None
    reserved_cost_nanos: int | None
    consumed_cost_nanos: int | None
    stop_confirmed: bool
    final_usage_confirmed: bool


@dataclass(frozen=True, slots=True)
class BudgetAccountRecord:
    """限額・既知消費・未決占用を分離し、超過時の負残額を隠さない。"""

    run_id: UUID
    policy: BudgetPolicy
    consumed_turns: int
    reserved_turns: int
    consumed_cost_nanos: int | None
    reserved_cost_nanos: int | None
    block_code: str | None
    row_version: int

    @property
    def remaining_turns(self) -> int:
        """主子の全承諾を引いた可用量だけを示す。"""

        return self.policy.max_turns - self.consumed_turns - self.reserved_turns

    @property
    def remaining_cost_nanos(self) -> int | None:
        """未設定の金銭限額をゼロ残高へ変換しない。"""

        if self.policy.max_cost_nanos is None:
            return None
        assert self.consumed_cost_nanos is not None and self.reserved_cost_nanos is not None
        return self.policy.max_cost_nanos - self.consumed_cost_nanos - self.reserved_cost_nanos


@dataclass(frozen=True, slots=True)
class BudgetReceiptResult:
    """異常も commit して返し、例外 rollback で停止理由を失わせない。"""

    account: BudgetAccountRecord
    execution: BudgetExecutionRecord
    disposition: str
