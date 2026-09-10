"""TaskSchedule の状態機、発火決議と read model を定義する (計画 §22)。

調度は新しい実行経路ではない。最終的に呼ぶのは即時実行と同一の `RunService.create_task_run` で
あり、ここが答えるのは「**いつ**・**誰の身分で**・**どの凍結設定で**」開始するかだけである。
Run 以降 (Segment/Attempt、交互、承認、effect) の不変条件は一切変えない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

# Schedule 名は一覧表示のための短い識別で、業務データではない。
MAX_SCHEDULE_NAME_LENGTH = 200
# 一回の tick で扱う到期 schedule の既定上限。Outbox と同じく「一度に処理する量」を有界にする。
DEFAULT_SCHEDULE_TICK_LIMIT = 20
# 公開観察と Worker の既定回復上限を同じ値から取得する。実行許可ではない。
DEFAULT_SCHEDULE_MAX_ATTEMPTS = 3


class ScheduleKind(StrEnum):
    """発火形態。監視条件は §22 D1 により作らない。"""

    ONCE = "ONCE"
    CRON = "CRON"


class ScheduleStatus(StrEnum):
    """TaskSchedule の lifecycle (§22 D9)。"""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    # 凍結した SkillVersion / 資源選択 / 作成者権限が失効した状態。設定を直せば手動で再開できる。
    ERROR = "ERROR"
    ARCHIVED = "ARCHIVED"


class ScheduleOutcome(StrEnum):
    """一回の到期処理が何を起こしたか。監査と一覧表示の両方で使う。"""

    RUN_CREATED = "RUN_CREATED"
    # 前回の Run がまだ非終態 (§22 D5)。次回へ送る。
    SKIPPED_OVERLAP = "SKIPPED_OVERLAP"
    # 凍結設定や作成者権限が失効した (§22 D4/D8)。ERROR へ落として発火を止める。
    FAILED_PRECONDITION = "FAILED_PRECONDITION"
    # 発火はしないが schedule 自体が尽きた (ONCE の消化、end_at / max_runs 到達)。
    COMPLETED = "COMPLETED"


# 終態。ここから戻せるのは ERROR だけで、それは利用者が設定を直した後の明示操作に限る。
TERMINAL_SCHEDULE_STATUSES = frozenset({ScheduleStatus.COMPLETED, ScheduleStatus.ARCHIVED})

ALLOWED_SCHEDULE_TRANSITIONS: dict[ScheduleStatus, frozenset[ScheduleStatus]] = {
    ScheduleStatus.ACTIVE: frozenset(
        {
            ScheduleStatus.PAUSED,
            ScheduleStatus.COMPLETED,
            ScheduleStatus.ERROR,
            ScheduleStatus.ARCHIVED,
        }
    ),
    ScheduleStatus.PAUSED: frozenset(
        {ScheduleStatus.ACTIVE, ScheduleStatus.COMPLETED, ScheduleStatus.ARCHIVED}
    ),
    # 設定を直した利用者が明示的に戻す経路だけを開ける。自動復帰はしない——失効原因が消えたかを
    # platform 側で判定できない (D4 は「黙って別の来源へ切り替えない」が主旨)。
    ScheduleStatus.ERROR: frozenset({ScheduleStatus.ACTIVE, ScheduleStatus.ARCHIVED}),
    ScheduleStatus.COMPLETED: frozenset({ScheduleStatus.ARCHIVED}),
    ScheduleStatus.ARCHIVED: frozenset(),
}


class ScheduleNotFoundError(LookupError):
    """Project 境界内に schedule が存在しないことを表す。"""


class ScheduleInvalidError(ValueError):
    """cron 式、時区、期間、上限のいずれかが受け付けられないことを表す。"""


class InvalidScheduleTransitionError(ValueError):
    """状態機が許さない遷移を表す。"""


class ScheduleConflictError(ValueError):
    """楽観ロックの衝突 (同時更新) を表す。"""


class ScheduleClaimLostError(RuntimeError):
    """失効した認領で新しい Run や結算を書き込む試みを拒否する。"""


class ScheduleOverlapError(RuntimeError):
    """同じ schedule に非終態の Run があるため新規作成を見送る。"""


class ScheduleOwnerUnavailableError(RuntimeError):
    """作成者の現在の権限で発火できないことを表す。"""


class ScheduleOccurrenceConflictError(ScheduleConflictError):
    """保存された原要求や結算と一致しない確認を拒否する。"""


class ScheduleActivityUnavailableError(RuntimeError):
    """破損した在途記録を未決なしへ読み替えず、公開観察を拒否する。"""


class ScheduleTracking(StrEnum):
    """在途台帳を読める形式かどうかだけを示し、再発火や停止を保証しない。"""

    TRACKED = "TRACKED"
    LEGACY_UNAVAILABLE = "LEGACY_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SchedulePendingOccurrence:
    """現在の PENDING 一件だけを公開し、原入力や lease credential は含めない。"""

    occurrence_id: UUID
    occurrence_at: datetime
    configuration_version: int
    created_at: datetime
    updated_at: datetime
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduleActivity:
    """同じ SELECT で観察した親の版と未決を運び、履歴や再実行権を表さない。"""

    schedule_id: UUID
    project_id: UUID
    row_version: int
    configuration_version: int
    tracking: ScheduleTracking
    checked_at: datetime
    automatic_attempt_limit: int
    pending: SchedulePendingOccurrence | None


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    """発火時刻を決める部分だけを切り出した値。予览と保存の双方で同じ検証を通す。"""

    kind: ScheduleKind
    timezone: str
    # CRON のときだけ必須。
    cron_expression: str | None
    # ONCE のときだけ必須。UTC で保持する。
    run_at: datetime | None
    # 双方で任意。到達すると COMPLETED。
    end_at: datetime | None
    max_runs: int | None


@dataclass(frozen=True, slots=True)
class CreateScheduleCommand:
    """Project 内に schedule を新規作成する命令。"""

    project_id: UUID
    name: str
    definition: ScheduleDefinition
    skill_version_id: UUID
    task_key: str
    input_json: dict[str, Any]
    sources: dict[str, str]
    created_by: UUID
    next_run_at: datetime


@dataclass(frozen=True, slots=True)
class UpdateScheduleCommand:
    """既存 schedule の定義・名称・冻结配置を差し替える命令。"""

    schedule_id: UUID
    project_id: UUID
    name: str
    definition: ScheduleDefinition
    input_json: dict[str, Any]
    sources: dict[str, str]
    next_run_at: datetime
    expected_row_version: int


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """API と Web が読む schedule の公開投影。Secret も接続情報も含まない。"""

    schedule_id: UUID
    project_id: UUID
    name: str
    kind: ScheduleKind
    status: ScheduleStatus
    timezone: str
    cron_expression: str | None
    run_at: datetime | None
    end_at: datetime | None
    max_runs: int | None
    skill_version_id: UUID
    task_key: str
    input_json: dict[str, Any]
    sources: dict[str, str]
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: UUID | None
    last_outcome: ScheduleOutcome | None
    last_error: str | None
    run_count: int
    missed_count: int
    created_by: UUID
    row_version: int
    created_at: datetime
    updated_at: datetime
    # 旧投影から新 writer の認領資格を補造しない。
    configuration_version: int = 1
    occurrence_protocol: int = 0


@dataclass(frozen=True, slots=True)
class SchedulePage:
    """Project 単位の schedule 一覧ページ。"""

    items: tuple[ScheduleRecord, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ClaimedSchedule:
    """原 occurrence の不変設定と、今回だけ有効な認領世代を運ぶ。"""

    schedule_id: UUID
    project_id: UUID
    occurrence_at: datetime
    skill_version_id: UUID
    task_key: str
    input_json: dict[str, Any]
    sources: dict[str, str]
    created_by: UUID
    # 時間規則だけの終端 (ONCE / end_at)。残枠は実際の Run 結算後に判定する。
    exhausted: bool
    # D6: 追いつかないと決めたぶんの回数。監査のために持ち回る。
    missed: int
    occurrence_id: UUID
    definition: ScheduleDefinition
    configuration_version: int
    claim_row_version: int
    snapshot_checksum: str
    worker_id: str
    lease_token: str = field(repr=False)
    lease_generation: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ScheduleTriggerResult:
    """一回の到期処理の結末。Worker の log と schedule 行の更新に使う。"""

    schedule_id: UUID
    occurrence_at: datetime
    outcome: ScheduleOutcome
    run_id: UUID | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleTickReport:
    """一回の tick 全体の集計。"""

    results: tuple[ScheduleTriggerResult, ...] = field(default_factory=tuple)

    @property
    def created(self) -> int:
        """Run を作った件数。"""

        return sum(1 for item in self.results if item.outcome is ScheduleOutcome.RUN_CREATED)

    @property
    def skipped(self) -> int:
        """重なりで見送った件数。"""

        return sum(1 for item in self.results if item.outcome is ScheduleOutcome.SKIPPED_OVERLAP)

    @property
    def failed(self) -> int:
        """前提失効で止めた件数。"""

        return sum(
            1 for item in self.results if item.outcome is ScheduleOutcome.FAILED_PRECONDITION
        )


def plan_schedule_transition(*, current: ScheduleStatus, target: ScheduleStatus) -> ScheduleStatus:
    """状態遷移の唯一の検証点。許されない遷移は例外にする。"""

    if target not in ALLOWED_SCHEDULE_TRANSITIONS[current]:
        raise InvalidScheduleTransitionError(
            f"Schedule transition is not allowed: {current} -> {target}"
        )
    return target


def schedule_idempotency_key(*, schedule_id: UUID, occurrence_at: datetime) -> str:
    """`(schedule, 発火時刻)` から決定的な Run idempotency key を導く (§22 D7)。

    tick が重なっても、recovery と同時に走っても、同一発火からは高々一つの Run しか生まれない。
    `create_task_run` 側の一意制約が最終的な保証で、CAS はその手前の無駄打ちを減らすだけ。
    """

    # host の local timezone に依存させない。UTC に正規化しないと、worker の TZ 設定が違うだけで
    # 同じ発火が別の key になり、二重発火の防止が崩れる。
    stamp = occurrence_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"schedule:{schedule_id}:{stamp}"
