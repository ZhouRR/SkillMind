"""Claude Agent SDK を用いた Skill Interpreter の一回構造化 completion transport。

本 transport は ANTHROPIC 資格情報と CLI subprocess を必要とし、offline 検証には含めない。
S3 が interpret service へ配線し、疎通は probe_claude_agent_sdk と実機 smoke で確認する。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ProcessError,
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
from skillmind.core.logging import log_event
from skillmind.skills.model_interpreter import (
    ModelCompletion,
    ModelProviderError,
    ModelStructuredOutputError,
)

_TRUNCATED_STOP_REASONS = frozenset({"max_tokens", "max_turns"})
_TRUNCATED_SUBTYPES = frozenset({"error_max_turns"})
_STRUCTURED_OUTPUT_FAILURE_SUBTYPES = frozenset({"error_max_structured_output_retries"})
_KNOWN_ERROR_SUBTYPES = frozenset({
    "success", "error_during_execution", "error_max_turns", "error_max_budget_usd",
    "error_max_structured_output_retries",
})
_KNOWN_SDK_ERROR_KINDS = frozenset({
    "ClaudeSDKError", "CLIConnectionError", "CLINotFoundError", "ProcessError",
    "CLIJSONDecodeError", "MessageParseError",
})
logger = logging.getLogger(__name__)


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
        text_message_id: str | None = None
        structured: Mapping[str, Any] | None = None
        truncated = False
        last_assistant_error: object = None
        try:
            async for message in query(prompt=user_message, options=options):
                if isinstance(message, AssistantMessage):
                    # CLI の max_output_tokens は SDK の古い Literal 型に未登録だが、parser は保持。
                    last_assistant_error = message.error
                    message_id = message.message_id
                    if not isinstance(message_id, str) or not message_id.strip():
                        message_id = None
                    # 同じ API 応答の block だけを結合する。再生成の thinking 首 block でも
                    # 旧本文を捨て、ID 不明の message は毎回独立させて打切り本文を混ぜない。
                    if message_id is None or message_id != text_message_id:
                        text_parts.clear()
                    text_message_id = message_id
                    text_parts.extend(
                        block.text for block in message.content if isinstance(block, TextBlock)
                    )
                elif isinstance(message, StreamEvent):
                    delta = extract_text_delta(message.event)
                    if delta is not None and on_text_delta is not None:
                        await on_text_delta(delta)
                elif isinstance(message, ResultMessage):
                    if message.subtype in _STRUCTURED_OUTPUT_FAILURE_SUBTYPES:
                        _log_result_failure(message, "structured_output_unavailable")
                        raise ModelStructuredOutputError(str(message.subtype))
                    # SDK 内の途中打切りを最終失敗と混同せず、HTTP/他の失敗を優先する。
                    result_truncated = message.api_error_status is None and (
                        message.subtype in _TRUNCATED_SUBTYPES
                        or (
                            message.subtype == "success"
                            and (
                                message.stop_reason in _TRUNCATED_STOP_REASONS
                                or (
                                    message.is_error and last_assistant_error == "max_output_tokens"
                                )
                            )
                        )
                    )
                    if message.is_error and not result_truncated:
                        _log_result_failure(message, "provider_error")
                        raise ModelProviderError(str(message.subtype))
                    if result_truncated:
                        _log_result_failure(message, "truncated_output")
                        truncated = True
                    structured = _structured_output(message)
        except ClaudeSDKError as error:
            # 例外本文/CLI stderr は資格情報やモデル内容を含み得るため、分類だけを残す。
            kind = type(error).__name__
            log_event(
                logger, logging.WARNING, "skill.interpret.completion_diagnostic",
                error_code="provider_error",
                provider_error_kind=(
                    kind if kind in _KNOWN_SDK_ERROR_KINDS else "OtherClaudeSDKError"
                ),
                provider_exit_code=(
                    error.exit_code
                    if isinstance(error, ProcessError) and type(error.exit_code) is int else None
                ),
            )
            raise ModelProviderError(type(error).__name__) from error
        return ModelCompletion(
            structured_output=structured,
            text="".join(text_parts) if text_parts else None,
            truncated=truncated,
        )


def _log_result_failure(message: ResultMessage, error_code: str) -> None:
    """既知 enum と有効な HTTP 状態だけを使い、result/errors 本文を出力しない。"""

    subtype = message.subtype
    api_error_status = message.api_error_status

    log_event(
        logger, logging.WARNING, "skill.interpret.completion_diagnostic",
        error_code=error_code,
        provider_result_subtype=subtype if subtype in _KNOWN_ERROR_SUBTYPES else "unrecognized",
        provider_api_error_status=(
            api_error_status
            if type(api_error_status) is int and 100 <= api_error_status <= 599 else None
        ),
    )


def _structured_output(message: ResultMessage) -> Mapping[str, Any] | None:
    """ResultMessage の structured_output が object の場合だけ返す。"""

    value = message.structured_output
    if isinstance(value, Mapping):
        return dict(value)
    return None
