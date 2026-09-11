"""Claude Agent SDK を用いた Skill Interpreter の一回構造化 completion transport。

本 transport は ANTHROPIC 資格情報と CLI subprocess を必要とし、offline 検証には含めない。
S3 が interpret service へ配線し、疎通は probe_claude_agent_sdk と実機 smoke で確認する。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    query,
)

from skillmind.agent.claude import (
    ClaudeRuntimeConfiguration,
    sanitized_agent_environment,
)
from skillmind.agent.claude_build import bundled_claude_build
from skillmind.agent.engine import extract_text_delta
from skillmind.agent.tool_policy import DENIED_BUILTIN_TOOLS
from skillmind.skills.model_interpreter import (
    ModelCompletion,
    ModelProviderError,
    ModelStructuredOutputError,
)

_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "max_turns"})
_TRUNCATED_SUBTYPES = frozenset({"error_max_turns"})
_STRUCTURED_OUTPUT_FAILURE_SUBTYPES = frozenset({"error_max_structured_output_retries"})


class ClaudeCompletionClient:
    """Interpreter を一回の read-only 構造化 completion として実行する production transport。"""

    def __init__(self, configuration: ClaudeRuntimeConfiguration) -> None:
        """Subprocess へ渡す許可済み環境変数だけを保持する。"""

        self._configuration = configuration

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        response_schema: Mapping[str, Any],
        model: str,
        parameters: Mapping[str, Any],
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelCompletion:
        """Tool を持たない一回の構造化 completion を実行し、SDK 例外を変換する。"""

        del parameters  # M0 interpreter は温度など追加生成 parameter を使わない。
        options = ClaudeAgentOptions(
            cli_path=bundled_claude_build().cli_path,
            system_prompt=system_prompt,
            model=model,
            tools=[],
            allowed_tools=[],
            disallowed_tools=sorted(DENIED_BUILTIN_TOOLS),
            mcp_servers={},
            strict_mcp_config=True,
            permission_mode="default",
            setting_sources=[],
            add_dirs=[],
            plugins=[],
            agents=None,
            max_turns=1,
            # Engine 実行と同じ単一 allowlist で subprocess 環境を隔離する。
            env=sanitized_agent_environment(self._configuration),
            output_format={"type": "json_schema", "schema": dict(response_schema)},
            # 進行の可視化を求められた時だけ partial message を受け、既定は従来通り一括で受ける。
            include_partial_messages=on_text_delta is not None,
        )
        text_parts: list[str] = []
        structured: Mapping[str, Any] | None = None
        truncated = False
        try:
            async for message in query(prompt=user_message, options=options):
                if isinstance(message, AssistantMessage):
                    text_parts.extend(
                        block.text for block in message.content if isinstance(block, TextBlock)
                    )
                elif isinstance(message, StreamEvent):
                    delta = extract_text_delta(message.event)
                    if delta is not None and on_text_delta is not None:
                        await on_text_delta(delta)
                elif isinstance(message, ResultMessage):
                    if message.subtype in _STRUCTURED_OUTPUT_FAILURE_SUBTYPES:
                        raise ModelStructuredOutputError(str(message.subtype))
                    if message.is_error and message.subtype not in _TRUNCATED_SUBTYPES:
                        raise ModelProviderError(str(message.subtype))
                    if (
                        message.subtype in _TRUNCATED_SUBTYPES
                        or message.stop_reason in _TRUNCATED_STOP_REASONS
                    ):
                        truncated = True
                    structured = _structured_output(message)
        except ClaudeSDKError as error:
            raise ModelProviderError(type(error).__name__) from error
        return ModelCompletion(
            structured_output=structured,
            text="".join(text_parts) if text_parts else None,
            truncated=truncated,
        )


def _structured_output(message: ResultMessage) -> Mapping[str, Any] | None:
    """ResultMessage の structured_output が object の場合だけ返す。"""

    value = message.structured_output
    if isinstance(value, Mapping):
        return dict(value)
    return None
