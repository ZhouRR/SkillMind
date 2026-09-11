"""Claude SDK の実呼出しと原 Result を SDK 非依存の用量観測へ写す。"""

from __future__ import annotations

from uuid import UUID, uuid4

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ResultMessage

from skillmind.agent.claude_build import require_bundled_cli
from skillmind.agent.domain import RunContext
from skillmind.agent.metering import (
    AgentInvocation,
    AgentInvocationMode,
    InvocationOptions,
    ResultUsageObservation,
    UsageValue,
)
from skillmind.core.hashing import canonical_json, sha256_hex


def capture_invocation(
    context: RunContext,
    *,
    session_id: str,
    prompt: str,
    options: ClaudeAgentOptions,
    mode: AgentInvocationMode,
    invocation_id: UUID | None = None,
) -> AgentInvocation:
    """execute/resume/fork の最終 options から観測範囲を固定し、父の hash を流用しない。"""

    build = require_bundled_cli(options.cli_path)
    return AgentInvocation(
        invocation_id=invocation_id if invocation_id is not None else uuid4(),
        project_id=context.project_id,
        run_id=context.run_id,
        run_attempt_id=context.run_attempt_id,
        user_id=context.user_id,
        session_id=session_id,
        mode=mode,
        parent_session_id=options.resume,
        prompt_checksum=sha256_hex(prompt),
        options=InvocationOptions(
            model=options.model,
            max_turns=options.max_turns,
            max_budget_usd=UsageValue.capture(options.max_budget_usd, allow_binary64=True),
            session_id=options.session_id,
            resume=options.resume,
            fork_session=options.fork_session,
            continue_conversation=options.continue_conversation,
            output_format_checksum=sha256_hex(canonical_json(options.output_format)),
            cli_checksum=build.cli_checksum,
        ),
        sdk_version=build.sdk_version,
        cli_version=build.cli_version,
    )


def capture_result_usage(
    invocation: AgentInvocation, message: ResultMessage
) -> ResultUsageObservation:
    """SDK parser が素通しする不正型を隔離し、任意 usage/body を観測へコピーしない。"""

    if message.session_id != invocation.session_id:
        raise ValueError("Claude result does not belong to the observed invocation")
    return ResultUsageObservation(
        invocation=invocation,
        turns=UsageValue.capture(message.num_turns),
        cost_usd=UsageValue.capture(message.total_cost_usd, allow_binary64=True),
    )
