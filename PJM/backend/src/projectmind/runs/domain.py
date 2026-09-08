"""Run lifecycle と idempotency に関する純粋な domain rule を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.effects.domain import (
    StoredChangeApproval,
    StoredChangeProposal,
    StoredEffectExecution,
)
from projectmind.runs.creation_request import CREATION_REQUEST_FIELD, TaskRunIntent


class RunStatus(StrEnum):
    """Run 全体の lifecycle status。"""

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    RETRY_PENDING = "RETRY_PENDING"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    # Release G より前の permission defer を読み出すためだけに残す歴史的 status。
    WAITING_PERMISSION = "WAITING_PERMISSION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunAttemptStatus(StrEnum):
    """Worker による個別実行試行の lifecycle status。"""

    CREATED = "CREATED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    DEFERRED = "DEFERRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LEASE_EXPIRED = "LEASE_EXPIRED"


class RunSegmentStatus(StrEnum):
    """ユーザー起点の一つの連続作業区間の lifecycle status。"""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunSegmentTrigger(StrEnum):
    """新しい Segment を作成した業務上の理由。"""

    INITIAL = "INITIAL"
    INTERACTION_RESPONSE = "INTERACTION_RESPONSE"
    INTERACTION_TIMEOUT = "INTERACTION_TIMEOUT"
    APPROVAL_RESPONSE = "APPROVAL_RESPONSE"
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"


class SessionContinuationMode(StrEnum):
    """前 Segment の Agent session を次 Segment で扱う方法。"""

    INITIAL = "INITIAL"
    RESUME = "RESUME"
    FORK = "FORK"
    REPLACE = "REPLACE"
    # 扇出の一路 (計画 §23 P3b)。他の四つが「前 Segment の会話をどう引き継ぐか」なのに対し、
    # BRANCH は会話を引き継がない——親の下で新規に始まり、親と**同時に**存在する。既存値の
    # どれかで代用すると、監査で「なぜ同じ Attempt に session が複数あるのか」が読めなくなる。
    # 利用者の回答で選べる継続方法ではないため、interaction/effect の継続 enum には入れない。
    BRANCH = "BRANCH"


class AgentSessionKind(StrEnum):
    """AgentSession が Run 骨格の本体か、扇出の一路か。

    `agent_sessions` の二つの一意制約 (Attempt ごと一つ / Run に ACTIVE 一つ) は
    **PRIMARY だけ**に掛かる。扇出の子は同じ Attempt の下に複数本が同時 ACTIVE で並ぶため、
    この区別が無いと子を保存した瞬間に制約違反になる (migration 0027)。
    """

    PRIMARY = "PRIMARY"
    SUBAGENT = "SUBAGENT"


class UserInteractionType(StrEnum):
    """Agent が公開 UI を通じて要求できる構造化 interaction 種別。"""

    CLARIFICATION = "CLARIFICATION"
    CHOICE = "CHOICE"
    REVIEW = "REVIEW"
    EFFECT_APPROVAL = "EFFECT_APPROVAL"


class UserInteractionStatus(StrEnum):
    """UserInteraction の追加式 lifecycle status。"""

    OPEN = "OPEN"
    RESPONDED = "RESPONDED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class RunCancellationState(StrEnum):
    """取消要求が受付済みか、Run が取消終態へ到達済みかを表す。"""

    REQUESTED = "REQUESTED"
    CANCELLED = "CANCELLED"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PREPARING, RunStatus.CANCELLED}),
    RunStatus.PREPARING: frozenset(
        {RunStatus.RUNNING, RunStatus.RETRY_PENDING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.RETRY_PENDING,
            RunStatus.WAITING_FOR_INPUT,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.WAITING_PERMISSION,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RETRY_PENDING: frozenset(
        {RunStatus.PREPARING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_PERMISSION: frozenset(
        {RunStatus.QUEUED, RunStatus.PREPARING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_FOR_INPUT: frozenset(
        {RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_FOR_APPROVAL: frozenset(
        {RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class IdempotencyConflictError(ValueError):
    """同一 idempotency key が異なる request 内容で再利用されたことを表す。"""


class InvalidRunTransitionError(ValueError):
    """Run state machine が許可しない状態遷移を表す。"""


class ConcurrentRunUpdateError(RuntimeError):
    """期待した row version と永続化済み version の不一致を表す。"""


class RunNotFoundError(LookupError):
    """指定された Run が存在しないことを表す。"""


class RunNotCancellableError(ValueError):
    """成功または失敗で確定済みの Run に取消が要求されたことを表す。"""


class TaskSourceSelectionError(ValueError):
    """Task の data source 選択が Manifest requirement に適合しないことを表す。"""


class LeaseValidationError(RuntimeError):
    """RunAttempt lease が不正、期限切れ、または更新不能であることを表す。"""


class RunCancellationRequestedError(RuntimeError):
    """有効な Worker も、取消要求後は新しい実行内容を凍結できないことを表す。"""


class InteractionConflictError(ValueError):
    """Interaction の version、状態または idempotency が競合したことを表す。"""


class InteractionExpiredError(ValueError):
    """期限切れ Interaction への回答が拒否されたことを表す。"""


class InteractionNotFoundError(LookupError):
    """Project/Run 境界内に Interaction が存在しないことを表す。"""


class InteractionResponseInvalidError(ValueError):
    """Interaction 種別に対して回答 shape が不正であることを表す。"""


@dataclass(frozen=True, slots=True)
class CreateRunCommand:
    """M0 Run を idempotent に作成するための server-side command。"""

    project_id: UUID
    task_id: UUID
    idempotency_key: str
    input_json: dict[str, Any]
    task_snapshot_json: dict[str, Any]
    permission_snapshot_json: dict[str, Any]
    selected_sources_json: dict[str, Any]
    limits_snapshot_json: dict[str, Any]
    trace_id: str | None
    skill_snapshots_json: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class CreatedRun:
    """作成または idempotent replay された Run の公開可能な情報。"""

    run_id: UUID
    project_id: UUID
    task_id: UUID
    status: RunStatus
    row_version: int
    created_at: datetime
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class CancelledRun:
    """取消 API が返す現在の Run snapshot と取消受付状態。"""

    run: CreatedRun
    cancellation: RunCancellationState


@dataclass(frozen=True, slots=True)
class StoredRunEvent:
    """SSE replay に使用する永続化済み RunEvent。"""

    run_id: UUID
    run_attempt_id: UUID | None
    agent_session_id: UUID | None
    sequence: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class PendingOutboxMessage:
    """Relay が排他的に処理する未公開 Outbox message。"""

    message_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    topic: str
    payload: dict[str, Any]
    publish_attempts: int


@dataclass(frozen=True, slots=True)
class ClaimedRun:
    """Worker lease と実行に必要な不変 Run snapshot。"""

    run_id: UUID
    run_attempt_id: UUID
    project_id: UUID
    actor_id: UUID
    attempt_no: int
    lease_token: str
    lease_expires_at: datetime
    row_version: int
    input_json: dict[str, Any]
    task_snapshot_json: dict[str, Any]
    permission_snapshot_json: dict[str, Any]
    selected_sources_json: dict[str, Any]
    limits_snapshot_json: dict[str, Any]
    skill_snapshots_json: tuple[dict[str, Any], ...] = ()
    # Release G 前の test fixture/歴史 path は明示 Segment を持たないため default を残す。
    run_segment_id: UUID | None = None
    segment_no: int = 1
    continuation_mode: SessionContinuationMode = SessionContinuationMode.INITIAL
    parent_agent_session_id: UUID | None = None
    parent_sdk_session_id: UUID | None = None
    parent_run_attempt_id: UUID | None = None
    checkpoint_json: dict[str, Any] | None = None
    checkpoint_checksum: str | None = None
    segment_objective_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    """PREPARING から RUNNING へ進めた後の event 採番開始値。"""

    row_version: int
    next_sequence: int


@dataclass(frozen=True, slots=True)
class AgentSessionMetadata:
    """RunAttempt に保存する SDK session の固定 metadata。"""

    cwd: str
    engine: str
    sdk_version: str
    cli_version: str
    model: str
    session_kind: AgentSessionKind = AgentSessionKind.PRIMARY
    parent_session_id: UUID | None = None
    continuation_mode: SessionContinuationMode = SessionContinuationMode.INITIAL
    checkpoint_checksum: str | None = None
    engine_options_checksum: str | None = None


@dataclass(frozen=True, slots=True)
class RunResultRecord:
    """ResultValidator を通過して終態 transaction へ渡す結果。"""

    output_schema: str
    result_kind: str
    data: dict[str, Any]
    evidence_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    change_proposal_refs: tuple[str, ...]
    optional_schema_identity: dict[str, Any]
    summary: str
    confidence: float | None
    needs_review: bool
    usage: dict[str, Any]
    cost: dict[str, Any]
    validation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredRunResult:
    """Read API が返す検証済み immutable Result。"""

    result_id: UUID
    output_schema: str
    result_kind: str
    data: dict[str, Any]
    evidence_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    change_proposal_refs: tuple[str, ...]
    optional_schema_identity: dict[str, Any]
    summary: str
    confidence: float | None
    needs_review: bool
    usage: dict[str, Any]
    cost: dict[str, Any]
    validation: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredToolCall:
    """Raw argument/result を除外した Run-scoped ToolCall projection。"""

    tool_call_id: UUID
    run_attempt_id: UUID
    agent_session_id: UUID
    tool_name: str
    capability: str
    provider: str
    arguments_summary: dict[str, Any]
    status: str
    duration_ms: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    """Run 内で参照可能な再定位情報と excerpt を表す Evidence projection。"""

    evidence_ref: str
    tool_call_id: UUID
    evidence_type: str
    source_uri: str
    source_locator: dict[str, Any]
    content_hash: str
    snapshot_uri: str | None
    excerpt: str | None
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredRunSkillSnapshot:
    """Run detail で追跡可能な SkillVersion と Manifest checksum binding。"""

    skill_version_id: UUID
    sort_order: int
    manifest_checksum: str
    config_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredRunSegment:
    """Run detail へ公開する Segment と Brief checkpoint の監査 projection。"""

    run_segment_id: UUID | None
    segment_no: int
    trigger_type: RunSegmentTrigger
    trigger_ref: UUID | None
    status: RunSegmentStatus
    objective: dict[str, Any]
    checkpoint: dict[str, Any]
    continuation_mode: SessionContinuationMode
    parent_agent_session_id: UUID | None
    task_brief_checksum: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredRunAttempt:
    """Lease token を除外した Segment 内の技術試行 projection。"""

    run_attempt_id: UUID
    run_segment_id: UUID | None
    attempt_no: int
    reason: str
    status: RunAttemptStatus
    worker_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAgentSession:
    """Session lineage と engine identity の公開可能な監査 projection。"""

    agent_session_id: UUID
    run_segment_id: UUID | None
    run_attempt_id: UUID
    # SDK が session を開く前に落ちた扇出 branch は None。捏造した ID を返さない。
    sdk_session_id: UUID | None
    parent_session_id: UUID | None
    continuation_mode: SessionContinuationMode
    # 扇出の一路かどうかは Run 詳細の時間線で本体と区別して見せるために要る。子の活動は主
    # Session の一回の ToolCall へ畳まれる (D3) ので、この行が唯一の追跡点になる。
    session_kind: AgentSessionKind
    checkpoint_checksum: str | None
    engine_options_checksum: str | None
    engine: str
    sdk_version: str
    cli_version: str
    model: str
    status: str
    usage: dict[str, Any]
    cost: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredInteractionResponse:
    """Actor と interaction version を含む追加式回答 projection。"""

    response_id: UUID
    actor_id: UUID
    interaction_version: int
    response: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredUserInteraction:
    """公開質問、選択肢、期限と任意回答をまとめた read projection。"""

    interaction_id: UUID
    run_segment_id: UUID
    agent_session_id: UUID
    interaction_type: UserInteractionType
    prompt: dict[str, Any]
    options: tuple[dict[str, Any], ...]
    required: bool
    expires_at: datetime
    status: UserInteractionStatus
    version: int
    continuation_mode: SessionContinuationMode
    checkpoint_checksum: str
    change_proposal_id: UUID | None
    response: StoredInteractionResponse | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RespondedInteraction:
    """回答受付後に作成された次 Segment と現在 Run snapshot。"""

    run: CreatedRun
    interaction_id: UUID
    response_id: UUID
    run_segment_id: UUID
    segment_no: int
    continuation_mode: SessionContinuationMode
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class RunDetail:
    """Project ownership 確認済み Run と Result/Evidence の read model。"""

    run: CreatedRun
    input: dict[str, Any]
    selected_sources: dict[str, Any]
    result: StoredRunResult | None
    tool_calls: tuple[StoredToolCall, ...]
    evidence: tuple[StoredEvidence, ...]
    skill_snapshots: tuple[StoredRunSkillSnapshot, ...] = ()
    output_schema: dict[str, Any] | None = None
    output_schema_checksum: str | None = None
    segments: tuple[StoredRunSegment, ...] = ()
    attempts: tuple[StoredRunAttempt, ...] = ()
    sessions: tuple[StoredAgentSession, ...] = ()
    interactions: tuple[StoredUserInteraction, ...] = ()
    change_proposals: tuple[StoredChangeProposal, ...] = ()
    approvals: tuple[StoredChangeApproval, ...] = ()
    effect_executions: tuple[StoredEffectExecution, ...] = ()


@dataclass(frozen=True, slots=True)
class RunHistoryItem:
    """Project 内の Run 一覧と再表示に必要な read model。"""

    run: CreatedRun
    input: dict[str, Any]
    selected_sources: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    result_summary: str | None
    result_confidence: float | None
    result_needs_review: bool | None


@dataclass(frozen=True, slots=True)
class TaskLastRun:
    """一つの task の最新 Run だけを切り出した read model。

    task catalog に「上次执行」を出すためのもの。画面側で Run 履歴の先頭 N 件を引いて突き合わせる
    実装だと、N 件より古い task が「未実行」と表示される——欠落ではなく**誤った値**になるため、
    絞り込みは SQL 側で行う。
    """

    task_id: UUID
    run_id: UUID
    status: RunStatus
    created_at: datetime
    finished_at: datetime | None
    result_summary: str | None


@dataclass(frozen=True, slots=True)
class RunHistoryPage:
    """Offset pagination 付き Project Run history。"""

    items: tuple[RunHistoryItem, ...]
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class RelayResult:
    """一回の Outbox relay 処理件数を表す。"""

    selected: int
    published: int
    failed: int


@dataclass(frozen=True, slots=True)
class RunTransition:
    """検証済み状態遷移で更新する値を保持する。"""

    status: RunStatus
    row_version: int
    started_at: datetime | None
    finished_at: datetime | None


def request_hash(command: CreateRunCommand) -> str:
    """新版の作成意図、または旧版の security snapshot 全体を識別する。"""

    if CREATION_REQUEST_FIELD in command.task_snapshot_json:
        intent = TaskRunIntent.from_json(command.task_snapshot_json[CREATION_REQUEST_FIELD])
        if (
            intent.project_id != command.project_id
            or derive_task_id(skill_version_id=intent.skill_version_id, task_key=intent.task_key)
            != command.task_id
            or command.task_snapshot_json.get("skill_version_id") != str(intent.skill_version_id)
            or command.task_snapshot_json.get("task_key") != intent.task_key
            or command.permission_snapshot_json.get("actor_id") != str(intent.actor_id)
            or canonical_json(command.input_json) != canonical_json(intent.input_json)
        ):
            raise ValueError("Creation request does not match its execution snapshot identity")
        return intent.fingerprint()

    payload = {
        "project_id": str(command.project_id),
        "task_id": str(command.task_id),
        "input": command.input_json,
        "task_snapshot": command.task_snapshot_json,
        "permission_snapshot": command.permission_snapshot_json,
        "selected_sources": command.selected_sources_json,
        "limits_snapshot": command.limits_snapshot_json,
        "skill_snapshots": command.skill_snapshots_json,
    }
    return sha256_hex(canonical_json(payload))


def derive_task_id(*, skill_version_id: UUID, task_key: str) -> UUID:
    """精確な (version, task) から決定的な task_id を導く。

    Run の idempotency 境界と、task catalog から Run 履歴を突き合わせる join key の**両方**が
    この値に依存する。導出を二箇所に書くと、片方だけ変えたときに「実行はできるのに履歴が
    紐づかない」という静かな不整合になるため、ここを唯一の実装とする。
    """

    return uuid5(NAMESPACE_URL, f"projectmind:task:{skill_version_id}:{task_key}")


def lease_token_hash(lease_token: str) -> str:
    """平文 lease token を保存・比較用の SHA-256 hash へ変換する。"""

    return sha256_hex(lease_token)


def plan_run_transition(
    *,
    current: RunStatus,
    target: RunStatus,
    row_version: int,
    started_at: datetime | None,
    finished_at: datetime | None,
    now: datetime,
) -> RunTransition:
    """Run state machine を検証し、timestamp と次 row version を計算する。"""

    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise InvalidRunTransitionError(f"Run transition is not allowed: {current} -> {target}")

    next_started_at = started_at
    if target is RunStatus.RUNNING and next_started_at is None:
        next_started_at = now
    next_finished_at = now if target in TERMINAL_RUN_STATUSES else finished_at
    return RunTransition(
        status=target,
        row_version=row_version + 1,
        started_at=next_started_at,
        finished_at=next_finished_at,
    )
