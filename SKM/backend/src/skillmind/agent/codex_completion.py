"""Codex SDK の tool 無効 completion を共有 Skill Interpreter 契約へ接続する。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from skillmind.agent.codex_diagnostics import codex_failure_detail
from skillmind.agent.codex_runtime import (
    CodexRuntimeConfiguration,
    codex_notifications,
    create_codex_client,
    start_codex,
)
from skillmind.agent.codex_schema import CodexOutputError, CodexOutputSchema
from skillmind.core.logging import log_event
from skillmind.skills.model_interpreter import (
    ModelCompletion,
    ModelInvalidOutputError,
    ModelProviderError,
)

logger = logging.getLogger(__name__)


class CodexCompletionClient:
    """設定済みの model/effort を保持し、native Schema 出力を候補として返す。"""

    def __init__(self, configuration: CodexRuntimeConfiguration) -> None:
        """API と Worker が共有する deployment 設定を固定する。"""

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
        """一つの native turn を実行し、切断時の別モデルへの自動再送を行わない。"""

        del parameters
        if model != self._configuration.model:
            raise ModelProviderError("Configured Codex model changed")
        output = CodexOutputSchema(response_schema)
        client = create_codex_client(self._configuration.client_config())
        try:
            await start_codex(client)
            thread = await asyncio.to_thread(
                client.thread_start,
                {
                    "model": model,
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "baseInstructions": system_prompt + output.instructions,
                    "ephemeral": True,
                },
            )
            if (
                thread.model != model
                or thread.reasoning_effort is None
                or thread.reasoning_effort.value != self._configuration.effort
            ):
                raise ModelProviderError("Codex did not retain the configured model and effort")
            turn = await asyncio.to_thread(
                client.turn_start,
                thread.thread.id,
                user_message,
                {
                    "model": model,
                    "effort": self._configuration.effort,
                    "outputSchema": output.schema,
                },
            )
            text = ""
            async for event in codex_notifications(client, turn.turn.id):
                method, params = event["method"], event["params"]
                if method == "item/agentMessage/delta" and on_text_delta is not None:
                    await on_text_delta(params["delta"])
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        text = item["text"]
                elif method == "turn/completed":
                    if params["turn"]["status"] != "completed":
                        detail = codex_failure_detail(params["turn"].get("error"))
                        log_event(
                            logger, logging.WARNING, "skill.interpret.completion_diagnostic",
                            error_code="provider_error", detail=detail,
                        )
                        raise ModelProviderError("Codex completion failed", detail=detail)
                    parsed = output.decode(text)
                    return ModelCompletion(
                        structured_output=parsed,
                        text=json.dumps(parsed, ensure_ascii=False),
                        truncated=False,
                    )
            raise ModelProviderError("Codex terminal response is missing")
        except Exception as error:
            # SDK 例外本文には応答/接続情報が含まれ得る。上位へ固定診断だけを返す。
            if isinstance(error, ModelProviderError):
                raise
            if isinstance(error, CodexOutputError):
                log_event(
                    logger, logging.WARNING, "skill.interpret.completion_diagnostic",
                    error_code="invalid_json", detail=error.detail,
                )
                raise ModelInvalidOutputError(
                    "Invalid Codex output", detail=error.detail
                ) from error
            detail = codex_failure_detail(getattr(error, "data", None))
            log_event(
                logger, logging.WARNING, "skill.interpret.completion_diagnostic",
                error_code="provider_error", detail=detail,
            )
            raise ModelProviderError("Codex completion transport failed", detail=detail) from error
        finally:
            await asyncio.to_thread(client.close)
