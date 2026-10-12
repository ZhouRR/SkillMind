"""承認済み HTTP request を一度送り、状態 code と原正文を記録する。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from skillmind.agent.http_source import HttpResourceError, HttpResourceSource
from skillmind.agent.materialization_storage import MaterializationError, write_workspace_file
from skillmind.agent.workspace import WorkspaceManager
from skillmind.core.hashing import sha256_hex
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.http_write import HTTP_WRITE, http_payload
from skillmind.effects.redmine import EffectProviderTransportError


class HttpResponseError(EffectProviderTransportError):
    """受信済み非 2xx を不明結果と区別し、Agent へ原 response の参照を渡す。"""

    def __init__(self, observation: EffectEvidenceDraft) -> None:
        """応答を例外文字列へ反射せず、保存専用の観測に限定する。"""
        super().__init__("http_response_error", retryable=False)
        self.observations = (observation,)


class HttpWriteProvider:
    """同じ Effect の再 claim では送信しない。業務判定は Agent に任せる。"""

    def __init__(
        self, *, source: HttpResourceSource, workspace_manager: WorkspaceManager,
        authorize: Callable[[ClaimedEffectExecution, str | None], Awaitable[Any]],
    ) -> None:
        """既存 Run workspace と共有認可を使い、response の byte を file に保存する。"""
        self._source, self._authorize = source, authorize
        self._workspace_manager = workspace_manager

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None,
    ) -> EffectProviderResult:
        """一回の response が確認根拠。202 は受理であり、業務完了ではない。"""
        if execution.capability_version != HTTP_WRITE or execution.provider != "http":
            raise ValueError("Invalid HTTP effect")
        if execution.attempt_no != 1:
            raise EffectProviderTransportError("http_original_result_unconfirmed", retryable=False)
        await self._authorize(execution, credential)
        payload = http_payload(
            execution.operation, execution.target, execution.changes,
            execution.integration_scope, execution.integration_config,
        )
        # 保存先を送信前に確定する。workspace が使えなければ外部変更を始めない。
        workspace = self._workspace_manager.initialize(execution.run_id)
        await self._authorize(execution, credential)
        try:
            response = await self._source.request(
                execution.integration_config, execution.integration_scope,
                payload["request"], credential, write=True,
            )
        except HttpResourceError:
            raise EffectProviderTransportError("http_result_unconfirmed", retryable=False) from None
        await self._authorize(execution, credential)
        path = f"workspace/resources/effects/{execution.effect_execution_id}/response.bin"
        try:
            await asyncio.to_thread(
                write_workspace_file, workspace.root, path, response.body,
                expected_hash="absent", reuse_identical=True,
            )
        except (MaterializationError, OSError):
            # 原要求は既に送信した。保存失敗でも mutation を自動再送しない。
            raise EffectProviderTransportError(
                "http_response_save_failed", retryable=False,
            ) from None
        snapshot = {
            "http_status": response.status, "headers": response.headers,
            "file": {"path": path, "size_bytes": len(response.body),
                     "content_hash": "sha256:" + sha256_hex(response.body)},
        }
        after = _evidence(execution, "response", snapshot)
        if not 200 <= response.status < 300:
            raise HttpResponseError(after)
        return EffectProviderResult(
            before=_evidence(execution, "request", payload), after=after, replayed=False,
            verification={
                "method": "HTTP_RESPONSE", "matched_paths": ["/request"],
                **snapshot, "business_verdict": "NOT_EVALUATED",
            },
        )


def _evidence(
    execution: ClaimedEffectExecution, phase: str, content: dict[str, Any],
) -> EffectEvidenceDraft:
    """送信要求と受信 response を分け、外部の事前状態や業務成功を捏造しない。"""
    return EffectEvidenceDraft(
        evidence_type="resource",
        source_uri=f"http-resource://{execution.integration_id}/{execution.effect_execution_id}/{phase}",
        source_locator={"effect_id": str(execution.effect_execution_id), "phase": phase},
        content=content, excerpt=None,
        metadata={"meaning": "HTTP request/response, not a business verdict"},
    )
