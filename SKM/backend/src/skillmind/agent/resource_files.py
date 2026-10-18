"""接続 client の完全な応答を Run file として提供する共通境界。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from jsonschema import Draft202012Validator

from skillmind.agent.materialization_storage import (
    MaterializationError,
    read_file,
    write_workspace_file,
)
from skillmind.agent.tool_diagnostics import schema_error_summary
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProvider,
    ToolProviderError,
)
from skillmind.core.hashing import canonical_json, sha256_hex


def require_file_access(context: RunToolContext) -> None:
    """実体の path 公開より先に同 Run の読取権を確認する。"""
    if context.run is None or "workspace.read/v1" not in context.run.permission_snapshot.get(
        "allowed_capabilities", []
    ):
        raise ToolProviderError(
            "scope_denied", "Resource files require workspace reading", retryable=False
        )


async def store_response(
    context: RunToolContext, data: bytes, *, suffix: str = "json"
) -> dict[str, Any]:
    """ToolCall ごとの別 file に書き、既存の Agent 草稿を上書きしない。"""
    require_file_access(context)
    if context.tool_call_id is None:
        raise ToolProviderError("unavailable", "Resource call identity is missing", retryable=False)
    path = f"workspace/resources/{context.tool_call_id}/response.{suffix}"
    try:
        await asyncio.to_thread(
            write_workspace_file,
            context.workspace.root,
            path,
            data,
            expected_hash="absent",
            reuse_identical=True,
        )
    except MaterializationError:
        raise ToolProviderError(
            "unavailable", "Resource response file could not be saved", retryable=False
        ) from None
    return {"path": path, "size_bytes": len(data), "content_hash": "sha256:" + sha256_hex(data)}


async def request_from_file(
    context: RunToolContext, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """要求 file を原 hash と照合して一度だけ読む。権限や接続指定は file から補わない。"""
    if "request_file" not in arguments:
        return dict(arguments)
    require_file_access(context)
    if set(arguments) - {"request_file", "expected_hash", "purpose", "response_mode"}:
        raise ToolProviderError(
            "invalid_request", "File and inline requests cannot be mixed", retryable=False
        )
    path = arguments["request_file"]
    if not isinstance(path, str) or not path.startswith(("workspace/", "output/")):
        raise ToolProviderError(
            "invalid_request", "Request file must be in workspace or output", retryable=False
        )
    try:
        raw = await asyncio.to_thread(
            read_file, context.workspace.root, path, max_bytes=16 * 1024 * 1024
        )
        if arguments.get("expected_hash") != "sha256:" + sha256_hex(raw):
            raise ValueError("changed")
        value = json.loads(raw)
        if not isinstance(value, dict) or "request_file" in value or "resource_key" in value:
            raise ValueError("request shape")
    except (ValueError, MaterializationError):
        raise ToolProviderError(
            "invalid_request", "Request file is invalid or changed", retryable=False
        ) from None
    return {
        **value,
        **({"response_mode": arguments["response_mode"]} if "response_mode" in arguments else {}),
    }


class ResourceFileProvider:
    """原 Provider の認可/意味を保ち、request/response の全文往復だけを file に置換する。"""

    def __init__(self, inner: ToolProvider, request_schema: Mapping[str, Any]) -> None:
        """内側へ渡す原契約も保持し、file input が検証を迂回しないようにする。"""
        self._inner, self._schema = inner, dict(request_schema)

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """内部の connection client が直接 I/O し、model は必要な箇所だけ読む。"""
        request = await request_from_file(context, arguments)
        mode = request.pop("response_mode", "inline")
        if mode == "file":
            require_file_access(context)
        if mode not in ("file", "inline"):
            raise ToolProviderError(
                "invalid_request", "/response_mode must be inline or file; "
                "the resource was not called.", retryable=False,
            )
        errors = sorted(
            Draft202012Validator(self._schema).iter_errors(request),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ToolProviderError(
                "invalid_request", "Resource request does not match its native tool contract: "
                + schema_error_summary(errors)
                + ". Correct the request file; the resource was not called.",
                retryable=False,
            )
        result = await self._inner.execute(context, request)
        if mode == "inline":
            return result
        file = await store_response(context, canonical_json(dict(result.response)).encode("utf-8"))
        # file は原観測の保存位置。配送方式だけで別の証拠 identity を増やさない。
        # 元内容の hash と応答 JSON file の hash は異なり得るため、双方を維持する。
        return ProviderToolResult(
            response={"status": "success", "provider": context.tool.provider, "file": file},
            evidence=tuple(
                replace(draft, metadata={**dict(draft.metadata or {}), "file": file})
                for draft in result.evidence
            ),
        )
