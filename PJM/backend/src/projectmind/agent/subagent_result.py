"""子実行の成功終端・identity・検証済み要約を一つの判定点に集約する。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from projectmind.agent.domain import AgentEvent, AgentEventType, RunContext
from projectmind.agent.result_validation import ResultValidator
from projectmind.agent.stream_lifecycle import close_async_stream

BranchStatus = Literal["COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"]
BranchFailureCode = Literal["branch_failed", "branch_timed_out", "branch_cancelled"]
MAX_BRANCH_SUMMARY = 20_000

_FAILED_TERMINALS = frozenset(
    {
        AgentEventType.ENGINE_FAILED,
        AgentEventType.SESSION_INTERRUPTED,
        AgentEventType.SESSION_DEFERRED,
        AgentEventType.INTERACTION_REQUESTED,
        AgentEventType.CHANGE_PROPOSED,
    }
)


@dataclass(frozen=True, slots=True)
class BranchOutcome:
    """検証済みの結末と、実際に観測/保存した二種類の Session identity。"""

    key: str
    outcome: BranchStatus
    summary: str
    sdk_session_id: UUID | None = None
    agent_session_id: UUID | None = None
    failure_code: BranchFailureCode | None = None


class SubagentProtocolError(ValueError):
    """失敗・欠落・衝突を成功 Tool response に変えてはならないことを表す。"""


class SubagentTranscript:
    """部分 text を完成扱いせず、一つの正常終端と所有者を検証する。"""

    def __init__(self, context: RunContext) -> None:
        """凍結 context と、この branch 内だけの sequence 水位を保持する。"""

        self._context = context
        self._last_sequence = context.sequence_start - 1
        self._saw_success = False
        self._structured_output: Any = None
        self.sdk_session_id: UUID | None = None

    async def consume(self, stream: AsyncIterator[AgentEvent]) -> None:
        """全 stream の正常終了を確認し、例外でも必ず所有 stream の解放を待つ。"""

        try:
            async for event in stream:
                self._observe(event)
        finally:
            await close_async_stream(stream)
        if not self._saw_success:
            raise SubagentProtocolError("Sub-agent success terminal is missing")

    def _observe(self, event: AgentEvent) -> None:
        """別実行の event、巻戻り、二つ目の終端、禁止 defer を拒否する。"""

        if (
            event.run_id != self._context.run_id
            or event.run_attempt_id != self._context.run_attempt_id
        ):
            raise SubagentProtocolError("Sub-agent event belongs to another execution")
        session_id = UUID(event.agent_session_id)
        if self.sdk_session_id is None:
            self.sdk_session_id = session_id
        if session_id != self.sdk_session_id or event.sequence <= self._last_sequence:
            raise SubagentProtocolError("Sub-agent event identity or sequence changed")
        self._last_sequence = event.sequence
        if self._saw_success:
            # disconnect の失敗等が成功 event の後から来た場合も、成功として返さない。
            raise SubagentProtocolError("Sub-agent emitted an event after its terminal")
        if event.event_type in _FAILED_TERMINALS:
            raise SubagentProtocolError("Sub-agent did not complete successfully")
        if event.event_type is AgentEventType.RESULT_COMPLETED:
            self._structured_output = deepcopy(event.payload.get("structured_output"))
            self._saw_success = True

    async def completed(self, key: str, validator: ResultValidator) -> BranchOutcome:
        """主と同じ Schema/機密/Evidence 所有検証を通った要約だけを採用する。"""

        if not self._saw_success:
            raise SubagentProtocolError("Sub-agent success terminal is missing")
        validated = await validator.validate_context(
            self._context, structured_output=self._structured_output
        )
        # validated.summary は Run 一覧用の短い preview。親が読む branch 本文まで
        # 280 文字へ切らず、検証済み payload に対して Tool 契約の上限だけを適用する。
        summary = validated.data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = validated.summary
        return BranchOutcome(
            key=key,
            outcome="COMPLETED",
            summary=summary.strip()[:MAX_BRANCH_SUMMARY],
            sdk_session_id=self.sdk_session_id,
        )

    def failed(
        self, key: str, outcome: BranchStatus, failure_code: BranchFailureCode
    ) -> BranchOutcome:
        """途中の text や内部例外を返さず、観測できた正しい Session identity を保つ。"""

        return BranchOutcome(
            key=key,
            outcome=outcome,
            summary="",
            sdk_session_id=self.sdk_session_id,
            failure_code=failure_code,
        )
