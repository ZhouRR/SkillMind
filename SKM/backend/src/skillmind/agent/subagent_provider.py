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

from skillmind.agent.domain import AgentEvent, RunContext
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.result_validation import ResultValidator
from skillmind.agent.subagent import (
    MAX_SUBAGENT_BRANCHES,
    SubagentBudget,
    SubagentCapabilityError,
    resolve_subagent_capabilities,
    split_budget,
)
from skillmind.agent.subagent_result import BranchOutcome, SubagentTranscript
from skillmind.agent.subagent_sessions import (
    SubagentSessionDraft,
    SubagentSessionRecorder,
)
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from skillmind.core.hashing import sha256_hex


class SubagentEngine(Protocol):
    """子 Session を一本走らせる engine port。主経路と同じ `execute` 契約だけを使う。"""

    def execute(self, context: RunContext) -> AsyncIterator[AgentEvent]:
        """凍結 context で新規 session を開始し、event を流す。"""

        ...


class SubagentDispatchProvider:
    """`subagent.dispatch/v1` を実装し、受限の子 Session を並行実行する。"""

    def __init__(
        self,
        *,
        engine: Callable[[], SubagentEngine],
        session_recorder: SubagentSessionRecorder,
        result_validator: ResultValidator,
        branch_timeout_seconds: float = 300.0,
    ) -> None:
        """engine の**遅延解決**と、一 branch あたりの打ち切り時間を保持する。

        engine → tool registry → 本 Provider → engine と参照が循環するため、engine 実体ではなく
        取得関数を受ける。解決は構築時ではなく**呼び出し時**に行うので、worker の組み立て順に
        依存しない。

        v1 は保存済み Session ID を必須とする。監査/出力検証の依存が無い構成は
        料金が発生する前に拒否し、契約に合わない省略応答や捏造 ID を作らない。
        """

        if branch_timeout_seconds <= 0:
            raise ValueError("Sub-agent branch timeout must be positive")
        if session_recorder is None or result_validator is None:
            raise ValueError("Sub-agent execution requires audit and result validation")
        self._engine = engine
        self._branch_timeout_seconds = branch_timeout_seconds
        self._session_recorder = session_recorder
        self._result_validator = result_validator

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
                resolve_subagent_capabilities(branch.capabilities, parent_allowed=parent_allowed)
                for branch in branches
            ]
            budget = split_budget(
                branches=len(branches),
                remaining_turns=parent.limits.max_turns,
                remaining_output_bytes=parent.limits.max_output_bytes,
            )
        except SubagentCapabilityError as error:
            raise ToolProviderError(error.code, str(error), retryable=False) from error

        # 一支が先に CancelledError で閉じても、残りの finally を再取消しない。
        # 同じ TaskGroup が全支の所有者となり、全 cleanup が終わるまで親取消を返さない。
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(self._run_branch(parent, branch, capabilities, budget))
                for branch, capabilities in zip(branches, granted, strict=True)
            ]
        completed = [task.result() for task in tasks]
        outcomes = await self._persist_sessions(context, completed)
        excerpt = _evidence_excerpt(objective, outcomes)
        evidence = EvidenceDraft(
            evidence_type="subagent-dispatch",
            source_uri=f"skillmind://runs/{context.run_id}/subagents",
            source_locator={"branches": [item.key for item in outcomes]},
            content_hash=f"sha256:{sha256_hex(excerpt)}",
            excerpt=excerpt[:2_000],
            metadata={
                "objective": objective[:1000],
                "branches": [
                    {
                        "key": item.key,
                        "outcome": item.outcome,
                        # _persist_sessions が全件の保存を確認した後だけ ID を返す。
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
        collected = SubagentTranscript(child)
        deadline = asyncio.timeout(self._branch_timeout_seconds)
        try:
            async with deadline:
                await collected.consume(self._engine().execute(child))
                return await collected.completed(branch.key, self._result_validator)
        except TimeoutError:
            if deadline.expired():
                return collected.failed(branch.key, "TIMED_OUT", "branch_timed_out")
            return collected.failed(branch.key, "FAILED", "branch_failed")
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # engine が理由なく CancelledError を投げても、user の取消とは断定しない。
            return collected.failed(branch.key, "FAILED", "branch_failed")
        except Exception:
            # 一路の失敗で全体を巻き戻さない。型名すら要約へ出さないのは、Provider 由来の
            # 内部情報を主 Agent の context へ流さないため。
            return collected.failed(branch.key, "FAILED", "branch_failed")

    async def _persist_sessions(
        self, context: RunToolContext, outcomes: Sequence[BranchOutcome]
    ) -> tuple[BranchOutcome, ...]:
        """一組の branch を一 transaction で保存し、採番 ID を結末へ結び直す。

        v1 の保存済み ID 要求を満たせなければ失敗に閉じる。結論の再生成は行わない。
        将来の監査降級は互換性を決めた別契約で扱い、ここで required を無視しない。
        """

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
            if (
                len(session_ids) != len(outcomes)
                or any(not isinstance(item, UUID) for item in session_ids)
                or len(set(session_ids)) != len(session_ids)
            ):
                raise ValueError("Sub-agent recorder returned invalid identities")
        except Exception:
            # DB の内部例外や不完全な結果を model へ渡さず、再実行も自動要求しない。
            raise ToolProviderError(
                "unavailable", "Sub-agent session audit could not be confirmed", retryable=False
            ) from None
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
