"""扇出子 Agent を並行実行する control Provider (計画 §23 P2)。

主 Agent の一回の Tool 呼び出しとして走り、結果は**主 Session 上の一つの ToolCall + Evidence**
へ収斂する。子 Session が各自 RunEvent を書かないのは、AGENTS の「Run 内 sequence は厳密単調増」を
階層番号へ作り変えずに済ませるため——Worker が唯一の event 書き手であるという構造を保つ
(計画 §23 D3)。

失敗は branch 単位で閉じ込める。一路が落ちても他路の結論は返す (D6)。全体を巻き戻すと、
高価な読み取りを何度もやり直すことになるうえ、主 Agent は「どこまで分かったか」を失う。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID

from projectmind.agent.domain import AgentEvent, AgentEventType, RunContext
from projectmind.agent.evidence import EvidenceDraft
from projectmind.agent.subagent import (
    MAX_SUBAGENT_BRANCHES,
    SubagentBudget,
    SubagentCapabilityError,
    resolve_subagent_capabilities,
    split_budget,
)
from projectmind.agent.subagent_sessions import (
    SubagentSessionDraft,
    SubagentSessionRecorder,
)
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from projectmind.core.hashing import sha256_hex

# 一 branch が生成してよい要約の上限。主 Agent の context を埋め尽くさないための実務的な上限で、
# contract 側の maxLength と一致させる。
_MAX_BRANCH_SUMMARY = 20_000


class SubagentEngine(Protocol):
    """子 Session を一本走らせる engine port。主経路と同じ `execute` 契約だけを使う。"""

    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """凍結 context で新規 session を開始し、event を流す。"""

        ...


@dataclass(frozen=True, slots=True)
class BranchOutcome:
    """一 branch の結末。失敗も値として返し、例外で全体を巻き戻さない。"""

    key: str
    outcome: str
    summary: str
    # engine が実際に開いた SDK session。SDK が起動する前に落ちた branch では None になる。
    sdk_session_id: UUID | None = None
    # 永続化後に採番される `agent_sessions.id`。保存前は None。
    agent_session_id: UUID | None = None
    failure_code: str | None = None


class SubagentDispatchProvider:
    """`subagent.dispatch/v1` を実装し、受限の子 Session を並行実行する。"""

    def __init__(
        self,
        *,
        engine: Callable[[], SubagentEngine],
        branch_timeout_seconds: float = 300.0,
        session_recorder: SubagentSessionRecorder | None = None,
    ) -> None:
        """engine の**遅延解決**と、一 branch あたりの打ち切り時間を保持する。

        engine → tool registry → 本 Provider → engine と参照が循環するため、engine 実体ではなく
        取得関数を受ける。解決は構築時ではなく**呼び出し時**に行うので、worker の組み立て順に
        依存しない。

        `session_recorder` を渡さない構成では子 Session を残さない。その場合 response の
        `agent_session_id` は**省略**する——引けない ID を返すくらいなら、無いと言う方がよい。
        """

        if branch_timeout_seconds <= 0:
            raise ValueError("Sub-agent branch timeout must be positive")
        self._engine = engine
        self._branch_timeout_seconds = branch_timeout_seconds
        self._session_recorder = session_recorder

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """要求された branch を検証し、並行実行して結論を集める。"""

        parent = context.run
        if parent is None:
            # 主 context 無しでは受限の子 context を作れない。広い権限で走らせるより閉じる。
            raise ToolProviderError(
                "unavailable", "Sub-agent dispatch has no frozen Run context", retryable=False
            )
        objective = str(arguments.get("objective") or "")
        branches = _branch_requests(arguments)
        parent_allowed = frozenset(
            item
            for item in parent.permission_snapshot.get("allowed_capabilities", [])
            if isinstance(item, str)
        )
        try:
            granted = [
                resolve_subagent_capabilities(
                    branch.capabilities, parent_allowed=parent_allowed
                )
                for branch in branches
            ]
            budget = split_budget(
                branches=len(branches),
                remaining_turns=parent.limits.max_turns,
                remaining_output_bytes=parent.limits.max_output_bytes,
            )
        except SubagentCapabilityError as error:
            raise ToolProviderError(error.code, str(error), retryable=False) from error

        completed = await asyncio.gather(
            *(
                self._run_branch(parent, branch, capabilities, budget)
                for branch, capabilities in zip(branches, granted, strict=True)
            )
        )
        outcomes = await self._persist_sessions(context, completed)
        excerpt = _evidence_excerpt(objective, outcomes)
        evidence = EvidenceDraft(
            evidence_type="subagent-dispatch",
            source_uri=f"projectmind://runs/{context.run_id}/subagents",
            source_locator={"branches": [item.key for item in outcomes]},
            content_hash=f"sha256:{sha256_hex(excerpt)}",
            excerpt=excerpt[:2_000],
            metadata={
                "objective": objective[:1000],
                "branches": [
                    {
                        "key": item.key,
                        "outcome": item.outcome,
                        # 引ける ID だけを載せる。保存できなかった路は session を出さない。
                        **(
                            {"session": str(item.agent_session_id)}
                            if item.agent_session_id is not None
                            else {}
                        ),
                    }
                    for item in outcomes
                ],
                "turns_per_branch": budget.turns_per_branch,
                "output_bytes_per_branch": budget.output_bytes_per_branch,
            },
        )
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "platform",
                "results": [_result_entry(item) for item in outcomes],
                "budget": {
                    "turns_per_branch": budget.turns_per_branch,
                    "output_bytes_per_branch": budget.output_bytes_per_branch,
                },
            },
            evidence=(evidence,),
        )

    async def _run_branch(
        self,
        parent: RunContext,
        branch: _BranchRequest,
        capabilities: tuple[str, ...],
        budget: SubagentBudget,
    ) -> BranchOutcome:
        """一 branch を受限 context で走らせ、失敗もこの中で閉じる。"""

        child = _derive_child_context(parent, branch, capabilities, budget)
        # engine が開いた SDK session を event から拾う。失敗した路でも、既に session が
        # 開いていたならその ID は本物なので残す——監査はそこから transcript を辿れる。
        collected = _BranchTranscript()
        try:
            await asyncio.wait_for(
                collected.consume(self._engine().execute(child)),
                timeout=self._branch_timeout_seconds,
            )
        except TimeoutError:
            return collected.failed(branch.key, "TIMED_OUT", "branch_timed_out")
        except asyncio.CancelledError:
            # 取消は group 全体へ効く (D6)。branch の結末として記録してから再送出はしない——
            # 呼び出し側の gather が他 branch も畳むため、ここでは値として返す。
            return collected.failed(branch.key, "CANCELLED", "branch_cancelled")
        except Exception:
            # 一路の失敗で全体を巻き戻さない。型名すら要約へ出さないのは、Provider 由来の
            # 内部情報を主 Agent の context へ流さないため。
            return collected.failed(branch.key, "FAILED", "branch_failed")
        return BranchOutcome(
            key=branch.key,
            outcome="COMPLETED",
            summary=collected.text()[:_MAX_BRANCH_SUMMARY],
            sdk_session_id=collected.sdk_session_id,
        )

    async def _persist_sessions(
        self, context: RunToolContext, outcomes: Sequence[BranchOutcome]
    ) -> tuple[BranchOutcome, ...]:
        """一組の branch を一 transaction で保存し、採番 ID を結末へ結び直す。

        保存に失敗しても dispatch そのものは失敗させない。子の結論は既に得られており、
        それを捨てる方が高くつく——ただし引けない ID を返すことは避け、`agent_session_id` を
        省いたまま返す。
        """

        if self._session_recorder is None:
            return tuple(outcomes)
        drafts = [
            SubagentSessionDraft(
                branch_key=item.key,
                outcome=item.outcome,
                sdk_session_id=item.sdk_session_id,
            )
            for item in outcomes
        ]
        try:
            session_ids = await self._session_recorder.record_branches(
                run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                drafts=drafts,
            )
        except Exception:
            return tuple(outcomes)
        return tuple(
            replace(item, agent_session_id=session_id)
            for item, session_id in zip(outcomes, session_ids, strict=True)
        )


@dataclass(frozen=True, slots=True)
class _BranchRequest:
    """検証済みの一 branch 要求。"""

    key: str
    objective: str
    capabilities: tuple[str, ...]


def _branch_requests(arguments: Mapping[str, Any]) -> tuple[_BranchRequest, ...]:
    """request の branch 配列を検証済み値へ写す。"""

    raw = arguments.get("branches")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or not raw:
        raise ToolProviderError(
            "invalid_request", "Sub-agent branches are required", retryable=False
        )
    if len(raw) > MAX_SUBAGENT_BRANCHES:
        raise ToolProviderError(
            "invalid_request",
            f"Sub-agent dispatch allows at most {MAX_SUBAGENT_BRANCHES} branches",
            retryable=False,
        )
    branches: list[_BranchRequest] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ToolProviderError(
                "invalid_request", "Sub-agent branch must be an object", retryable=False
            )
        key = str(item.get("key") or "")
        objective = str(item.get("objective") or "")
        if not key or not objective:
            raise ToolProviderError(
                "invalid_request", "Sub-agent branch needs a key and an objective", retryable=False
            )
        if key in seen:
            # 同じ key が二つあると、主 Agent が結果を取り違える。
            raise ToolProviderError(
                "invalid_request", f"Duplicate sub-agent branch key: {key}", retryable=False
            )
        seen.add(key)
        raw_capabilities = item.get("capabilities")
        capabilities = tuple(
            value
            for value in (raw_capabilities if isinstance(raw_capabilities, Sequence) else ())
            if isinstance(value, str)
        )
        branches.append(_BranchRequest(key=key, objective=objective, capabilities=capabilities))
    return tuple(branches)


def _derive_child_context(
    parent: RunContext,
    branch: _BranchRequest,
    capabilities: tuple[str, ...],
    budget: SubagentBudget,
) -> RunContext:
    """主 context から**狭める方向にだけ**子 context を派生させる (計画 §23 D7)。

    `RunContext` は frozen dataclass のため、`replace` で作った子から親の権限や上限へ
    戻る経路は無い。
    tools も付与能力で絞り、prompt は branch の目的だけにする。
    """

    allowed = frozenset(capabilities)
    return replace(
        parent,
        prompt=_branch_prompt(branch),
        permission_snapshot={
            **parent.permission_snapshot,
            "allowed_capabilities": sorted(allowed),
            # 子は扇出も交互も効果も持たない。snapshot 上でも明示して監査を読みやすくする。
            "subagent_of": str(parent.run_id),
            "subagent_branch": branch.key,
        },
        limits=replace(
            parent.limits,
            max_turns=budget.turns_per_branch,
            max_output_bytes=budget.output_bytes_per_branch,
        ),
        tools=tuple(tool for tool in parent.tools if tool.capability in allowed),
    )


def _branch_prompt(branch: _BranchRequest) -> str:
    """branch の目的だけを渡す prompt。主 Agent の文脈は持ち込まない。"""

    return (
        f"{branch.objective}\n\n"
        "You are a bounded read-only sub-analysis of a larger run. Report only what you can "
        "support with what you read. You cannot write, ask the user, propose changes, or start "
        "further sub-analyses."
    )


class _BranchTranscript:
    """子 Session の stream から要約と SDK session ID を拾う。

    打ち切り・取消・失敗のどれで抜けても、その時点までに判明した SDK session ID を保持したい。
    生成関数の戻り値だと例外で失われるため、外側の可変状態として持つ。
    """

    def __init__(self) -> None:
        """未受信の状態で開始する。"""

        self._parts: list[str] = []
        self.sdk_session_id: UUID | None = None

    async def consume(self, stream: AsyncIterator[AgentEvent]) -> None:
        """stream を最後まで読み、text と session identity を蓄える。"""

        async for event in stream:
            if self.sdk_session_id is None:
                try:
                    self.sdk_session_id = UUID(event.agent_session_id)
                except ValueError:
                    # AgentEvent は生成時に UUID を検証済み。ここへ来るのは fake engine だけで、
                    # 実在しない ID を保存するくらいなら未取得のままにする。
                    self.sdk_session_id = None
            # TEXT_DELTA は逐次断片、TEXT_COMPLETED は確定文。両方拾うと二重になるため、
            # 確定文が来た時点でそれまでの断片を捨てて置き換える。
            if event.event_type is AgentEventType.TEXT_COMPLETED:
                text = event.payload.get("text")
                if isinstance(text, str):
                    self._parts = [text]
                continue
            if event.event_type is AgentEventType.TEXT_DELTA:
                payload = event.payload.get("text")
                if isinstance(payload, str):
                    self._parts.append(payload)

    def text(self) -> str:
        """蓄えた断片を連結する。"""

        return "".join(self._parts).strip()

    def failed(self, key: str, outcome: str, failure_code: str) -> BranchOutcome:
        """失敗した結末を作る。要約は返さないが session identity は残す。"""

        return BranchOutcome(
            key=key,
            outcome=outcome,
            summary="",
            sdk_session_id=self.sdk_session_id,
            failure_code=failure_code,
        )


def _result_entry(outcome: BranchOutcome) -> dict[str, Any]:
    """branch の結末を response contract の形へ写す。"""

    entry: dict[str, Any] = {
        "key": outcome.key,
        "outcome": outcome.outcome,
        "summary": outcome.summary,
    }
    if outcome.agent_session_id is not None:
        # 保存済みの行を指す ID だけを載せる。以前は毎回 uuid4 を返しており、監査でどこからも
        # 引けない参照になっていた (計画 §23 P3b)。
        entry["agent_session_id"] = str(outcome.agent_session_id)
    if outcome.failure_code is not None:
        entry["failure_code"] = outcome.failure_code
    return entry


def _evidence_excerpt(objective: str, outcomes: Sequence[BranchOutcome]) -> str:
    """Evidence 本文。どの branch がどう終わったかだけを残す。"""

    lines = [f"objective: {objective[:500]}"]
    lines.extend(f"{item.key}: {item.outcome}" for item in outcomes)
    return "\n".join(lines)


__all__ = [
    "BranchOutcome",
    "SubagentDispatchProvider",
    "SubagentEngine",
]
