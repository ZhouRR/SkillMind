"""承認済み HTTP request を一度送り、実応答と回読を記録する。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from skillmind.agent.http_source import HttpResourceError, HttpResourceSource, HttpResponse
from skillmind.agent.tool_sequence import matches_checks
from skillmind.core.hashing import sha256_hex
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.http_write import HTTP_WRITE, http_payload
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
    EffectProviderVerificationError,
)


class HttpWriteProvider:
    """再 claim は送信しない。汎用 GET の一致から原操作成功を推測しない。"""

    def __init__(
        self,
        *,
        source: HttpResourceSource,
        authorize: Callable[[ClaimedEffectExecution, str | None], Awaitable[Any]],
    ) -> None:
        """他 Provider と同じ段階認可を要求する。"""
        self._source, self._authorize = source, authorize

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """preflight → single send → read-back。HTTP 202 は完成として扱わない。"""
        if execution.capability_version != HTTP_WRITE or execution.provider != "http":
            raise ValueError("Invalid HTTP effect")
        if execution.attempt_no != 1:
            raise EffectProviderTransportError("http_original_result_unconfirmed", retryable=False)
        await self._authorize(execution, credential)
        payload = http_payload(
            execution.operation,
            execution.target,
            execution.changes,
            execution.integration_scope,
            execution.integration_config,
        )

        async def send(request: dict[str, Any], *, write: bool = False) -> HttpResponse:
            """すべての I/O の前後で取消・撤権・lease を確認する。"""
            await self._authorize(execution, credential)
            result = await self._source.request(
                execution.integration_config,
                execution.integration_scope,
                request,
                credential,
                write=write,
            )
            await self._authorize(execution, credential)
            return result

        try:
            before = await send(payload["read_back"])
            if (
                before.status >= 300
                or "sha256:" + sha256_hex(before.body) != execution.precondition["revision"]
            ):
                raise EffectProviderStaleError("HTTP precondition changed")
            request = dict(payload["request"])
            # ETag は同じ target の GET で観測した時だけ使用する。
            if before.headers.get("etag") and request["path"] == payload["read_back"]["path"]:
                headers = {
                    k: v for k, v in request.get("headers", {}).items() if k.lower() != "if-match"
                }
                request["headers"] = {**headers, "If-Match": before.headers["etag"]}
            sent = await send(request, write=True)
            if not 200 <= sent.status < 300 or sent.status == 202:
                raise EffectProviderTransportError("http_write_not_confirmed", retryable=False)
            after = await send(payload["read_back"])
            if after.status >= 300 or not matches_checks(json.loads(after.body), payload["checks"]):
                raise EffectProviderVerificationError("HTTP read-back did not match")
        except (HttpResourceError, json.JSONDecodeError, UnicodeDecodeError):
            raise EffectProviderTransportError("http_result_unconfirmed", retryable=False) from None
        return EffectProviderResult(
            before=_evidence(execution, before, "before"),
            after=_evidence(execution, after, "after"),
            replayed=False,
            verification={
                "method": "READ_BACK",
                "matched_paths": ["/request"],
                "http_status": sent.status,
                "response_hash": "sha256:" + sha256_hex(sent.body),
                "business_verdict": "NOT_EVALUATED",
            },
        )


def _evidence(
    execution: ClaimedEffectExecution, response: HttpResponse, phase: str
) -> EffectEvidenceDraft:
    """実応答の hash と status を保存し、機密 header/body を通常履歴へ複写しない。"""
    return EffectEvidenceDraft(
        evidence_type="resource",
        source_uri=f"http-resource://{execution.integration_id}/{execution.effect_execution_id}/{phase}",
        source_locator={"effect_id": str(execution.effect_execution_id), "phase": phase},
        content={
            "http_status": response.status,
            "content_hash": "sha256:" + sha256_hex(response.body),
        },
        excerpt=None,
        metadata={"meaning": "HTTP response and verified current state, not a business verdict"},
    )
