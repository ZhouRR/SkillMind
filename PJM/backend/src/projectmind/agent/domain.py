"""Business 層と Agent SDK adapter の間で共有する不変契約を定義する。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from projectmind.runs.input_snapshot import InputFileSeal

if TYPE_CHECKING:
    from projectmind.agent.metering import AgentInvocation


class AgentEventType(StrEnum):
    """SDK 固有 message を隠蔽した監査可能な Agent event 種別。"""

    SESSION_STARTED = "SESSION_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    TEXT_DELTA = "TEXT_DELTA"
    TEXT_COMPLETED = "TEXT_COMPLETED"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    PERMISSION_RESOLVED = "PERMISSION_RESOLVED"
    EVIDENCE_CREATED = "EVIDENCE_CREATED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    USAGE_UPDATED = "USAGE_UPDATED"
    SESSION_DEFERRED = "SESSION_DEFERRED"
    SESSION_INTERRUPTED = "SESSION_INTERRUPTED"
    SESSION_STORE_DEGRADED = "SESSION_STORE_DEGRADED"
    SEGMENT_STARTED = "SEGMENT_STARTED"
    SEGMENT_COMPLETED = "SEGMENT_COMPLETED"
    INTERACTION_REQUESTED = "INTERACTION_REQUESTED"
    INTERACTION_RESPONDED = "INTERACTION_RESPONDED"
    INTERACTION_EXPIRED = "INTERACTION_EXPIRED"
    CHANGE_PROPOSED = "CHANGE_PROPOSED"
    EFFECT_APPROVED = "EFFECT_APPROVED"
    EFFECT_REJECTED = "EFFECT_REJECTED"
    EFFECT_APPLIED = "EFFECT_APPLIED"
    EFFECT_FAILED = "EFFECT_FAILED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    SESSION_RESUMED = "SESSION_RESUMED"
    SESSION_FORKED = "SESSION_FORKED"
    SESSION_REPLACED = "SESSION_REPLACED"
    RESULT_COMPLETED = "RESULT_COMPLETED"
    ENGINE_FAILED = "ENGINE_FAILED"


class EngineHealthStatus(StrEnum):
    """Agent engine が新規 Run を受け付けられるかを表す状態。"""

    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class AgentSessionRef:
    """SDK session を ProjectMind の Run と関連付ける参照。"""

    run_id: UUID
    run_attempt_id: UUID
    session_id: str

    def __post_init__(self) -> None:
        """SDK が要求する UUID 形式以外の session 参照を拒否する。"""

        try:
            UUID(self.session_id)
        except ValueError as error:
            raise ValueError("Agent session ID must be a UUID") from error


@dataclass(frozen=True, slots=True)
class MaterializedResource:
    """物化した 1 資源根の、Agent がそのまま使える位置と来歴 (計画 §19 W6)。

    path は `workspace.read` / `workspace.search` へ渡す表記 (`input/...`) で持つ。Agent に
    「どこに何があるか」を伝える唯一の材料であり、物化器が実際に書いた結果からのみ作る
    (blueprint から推測すると、未配線環境で存在しない directory を案内してしまう)。
    """

    requirement_key: str
    kind: str
    provider: str
    root: str
    manifest_path: str
    index_path: str
    history_path: str | None
    revision: str | None
    files: int
    skipped: int


@dataclass(frozen=True, slots=True)
class RunWorkspace:
    """一つの Run に固定された作業領域の絶対 path。"""

    root: Path
    cwd: Path
    input_dir: Path
    output_dir: Path
    temp_dir: Path
    input_files: tuple[InputFileSeal, ...] | None = None
    input_file_index: Mapping[str, InputFileSeal] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """相対 path や root 外 path を拒否して workspace 境界を固定する。"""

        paths = (self.root, self.cwd, self.input_dir, self.output_dir, self.temp_dir)
        if any(not path.is_absolute() for path in paths):
            raise ValueError("Run workspace paths must be absolute")
        root = self.root.resolve(strict=False)
        if any(not path.resolve(strict=False).is_relative_to(root) for path in paths[1:]):
            raise ValueError("Run workspace paths must stay under the run root")
        index = {item.path: item for item in self.input_files or ()}
        if len(index) != len(self.input_files or ()):
            raise ValueError("Run input receipt contains duplicate files")
        object.__setattr__(self, "input_file_index", MappingProxyType(index))


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Agent loop に適用する Run 作成時点の resource 上限。"""

    max_turns: int
    wall_timeout_seconds: int
    max_output_bytes: int
    max_budget_usd: float | None = None

    def __post_init__(self) -> None:
        """無制限実行につながるゼロ以下の値を拒否する。"""

        if min(self.max_turns, self.wall_timeout_seconds, self.max_output_bytes) <= 0:
            raise ValueError("Run limits must be positive")
        if self.max_budget_usd is not None and self.max_budget_usd <= 0:
            raise ValueError("Run budget must be positive")


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """Run に登録済みの一つの MCP Tool と入力契約を表す。"""

    capability: str
    sdk_name: str
    provider: str
    integration_id: UUID | None
    input_schema: Mapping[str, Any]
    read_only: bool = True
    binding_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class RunContext:
    """AgentEngine に渡す、作成後に変更できない実行 snapshot。"""

    run_id: UUID
    run_attempt_id: UUID
    project_id: UUID
    user_id: UUID
    prompt: str
    task_snapshot: Mapping[str, Any]
    skill_snapshots: tuple[Mapping[str, Any], ...]
    resolved_sources: Mapping[str, Any]
    permission_snapshot: Mapping[str, Any]
    workspace: RunWorkspace
    limits: RunLimits
    result_schema: Mapping[str, Any]
    tools: tuple[RegisteredTool, ...]
    model: str
    sequence_start: int = 1
    trace: Mapping[str, Any] = field(default_factory=dict)
    # Prompt の組成根拠となる AgentTaskBrief と canonical checksum。prompt は Brief から
    # 描画されるため、この二つが Agent へ渡した指示内容の監査正本になる。
    task_brief: Mapping[str, Any] = field(default_factory=dict)
    task_brief_checksum: str = ""
    # 受信調整者が原束縛から再読取した記述子。trace やモデル入力から復元しない。
    prepared_invocation: AgentInvocation | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Attempt を跨ぐ event 採番の開始値が正数であることを保証する。"""

        if self.sequence_start <= 0:
            raise ValueError("Agent event sequence start must be positive")


@dataclass(frozen=True, slots=True)
class ResumeContext:
    """同一 Run snapshot で既存 session を再開する入力。"""

    run: RunContext
    session: AgentSessionRef
    input_text: str | None = None


@dataclass(frozen=True, slots=True)
class ForkContext:
    """読み取り専用 Run の session を分岐する入力。"""

    run: RunContext
    parent_session: AgentSessionRef
    input_text: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Run 内の単調増加 sequence を持つ SDK 非依存 event。"""

    run_id: UUID
    run_attempt_id: UUID
    agent_session_id: str
    sequence: int
    occurred_at: datetime
    event_type: AgentEventType
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        """永続化前に不正な session ID と sequence を拒否する。"""

        if self.sequence <= 0:
            raise ValueError("Agent event sequence must be positive")
        try:
            UUID(self.agent_session_id)
        except ValueError as error:
            raise ValueError("Agent event session ID must be a UUID") from error


@dataclass(frozen=True, slots=True)
class EngineHealth:
    """Engine と同梱 CLI の互換状態を返す診断結果。"""

    status: EngineHealthStatus
    engine: str
    sdk_version: str
    cli_version: str
    details: Mapping[str, Any] = field(default_factory=dict)


class AgentEngine(Protocol):
    """Business 層が依存する SDK 非依存の Agent 実行 port。"""

    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """新規 session で Run を実行し、正規化 event を逐次返す。"""

        ...

    def resume(self, context: ResumeContext) -> AsyncIterator[AgentEvent]:
        """同一 snapshot の既存 session を新しい RunAttempt で再開する。"""

        ...

    def fork(self, context: ForkContext) -> AsyncIterator[AgentEvent]:
        """読み取り専用 session を候補結果用に分岐する。"""

        ...

    async def interrupt(self, session_ref: AgentSessionRef) -> None:
        """活動中 session を中断し、終端 message まで drain する。"""

        ...

    async def health(self) -> EngineHealth:
        """SDK と CLI の互換性を model 呼び出しなしで確認する。"""

        ...
