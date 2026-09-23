"""Codex SDK の tool 無効 completion を共有 Skill Interpreter 契約へ接続する。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from typing import Any

from openai_codex.client import CodexClient

from skillmind.agent.codex_diagnostics import codex_failure_detail
from skillmind.agent.codex_runtime import (
    CodexRuntimeConfiguration,
    codex_notifications,
    create_codex_client,
    start_codex,
)
from skillmind.agent.codex_schema import (
    CodexOutputError,
    CodexOutputSchema,
    NativeCodexOutputSchema,
)
from skillmind.agent.json_output_guard import JsonWhitespaceGuard
from skillmind.core.logging import log_event
from skillmind.skills.candidate import CANDIDATE_SCHEMA_ID
from skillmind.skills.direct_candidate import CANDIDATE_SCHEMA_ID as DIRECT_SCHEMA_ID
from skillmind.skills.interpreter_execution import MODEL_OUTPUT_WHITESPACE_LIMIT
from skillmind.skills.model_interpreter import (
    ModelCompletion,
    ModelInvalidOutputError,
    ModelProviderError,
)
from skillmind.skills.runtime_profile import validate_interpreter_parameters

logger = logging.getLogger(__name__)
_INTERRUPT_TIMEOUT_SECONDS = 5


def _close_completion_client(client: CodexClient) -> None:
    """固定 SDK の kill 後も子を reap し、呼出し終了後に process を残さない。"""

    # SDK 0.156.0 の close は terminate 待機に失敗すると kill だけで戻る。元 handle を保持し、
    # 同じ呼出しの子だけを wait する。PID 再検索や別 Worker の停止は行わない。
    process = client._proc
    try:
        client.close()
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


async def _interrupt_completion(client: CodexClient, thread_id: str, turn_id: str) -> None:
    """中断要求の応答待機を制限し、失敗時も finally の process 回収へ必ず進む。"""

    try:
        async with asyncio.timeout(_INTERRUPT_TIMEOUT_SECONDS):
            await asyncio.to_thread(client.turn_interrupt, thread_id, turn_id)
    except Exception:
        # RPC の応答だけでは upstream の停止を証明しない。原呼出しの close が後続で必須。
        log_event(
            logger, logging.WARNING, "skill.interpret.completion_diagnostic",
            error_code="provider_error", detail="codex:interrupt_unconfirmed",
        )


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

        try:
            validate_interpreter_parameters(parameters)
        except ValueError as error:
            raise ModelProviderError("Per-call interpreter parameters are not supported") from error
        if model != self._configuration.model:
            raise ModelProviderError("Configured Codex model changed")
        output = (
            NativeCodexOutputSchema(response_schema)
            if response_schema.get("$id") in {CANDIDATE_SCHEMA_ID, DIRECT_SCHEMA_ID}
            else CodexOutputSchema(response_schema)
        )
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
            guard = JsonWhitespaceGuard()
            message_id: str | None = None
            async with aclosing(codex_notifications(client, turn.turn.id)) as events:
                async for event in events:
                    method, params = event["method"], event["params"]
                    if method == "item/agentMessage/delta":
                        if params.get("itemId") != message_id:
                            guard = JsonWhitespaceGuard()
                            message_id = params.get("itemId")
                        if guard.exceeded(params["delta"]):
                            log_event(
                                logger, logging.WARNING, "skill.interpret.completion_diagnostic",
                                error_code="provider_error", detail=MODEL_OUTPUT_WHITESPACE_LIMIT,
                            )
                            await _interrupt_completion(client, thread.thread.id, turn.turn.id)
                            raise ModelProviderError(
                                "Model output stalled on consecutive whitespace",
                                detail=MODEL_OUTPUT_WHITESPACE_LIMIT,
                            )
                        if on_text_delta is not None:
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
            cleanup = asyncio.create_task(asyncio.to_thread(_close_completion_client, client))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # 外側の取消でも SDK process の回収を完了してから呼出しを解放する。
                await asyncio.shield(cleanup)
                raise
