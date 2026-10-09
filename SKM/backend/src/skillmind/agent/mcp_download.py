"""原 MCP 観測が返した同一 origin の参照だけを、登録資格で GET する。"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.http_source import HttpResourceError, HttpResourceSource
from skillmind.agent.materialization_storage import MaterializationError, read_file
from skillmind.agent.mcp_provider import McpReadProvider
from skillmind.agent.mcp_source import StreamableHttpMcpSource
from skillmind.agent.resource_files import require_file_access, store_response
from skillmind.agent.run_binding import BoundRunResource
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import Evidence, ToolCall
from skillmind.integrations.http_resource import validate_path
from skillmind.integrations.mcp_tools import tool_access
from skillmind.integrations.secrets import DeploymentSecretResolver

CAPABILITY = "mcp.download/v1"


def _point(value: Any, pointer: str) -> Any:
    """原 JSON の明示された位置だけを読み、式や部分文字列検索をしない。"""
    if len(pointer) > 2048 or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)+", pointer) is None:
        raise ValueError("Invalid observation pointer")
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if re.fullmatch(r"0|[1-9][0-9]*", key) is None:
                raise ValueError("Invalid array index")
            value = value[int(key)]
        else:
            value = value[key]
    return value


def observed_path(value: Any, pointer: str, endpoint: str) -> str:
    """元応答の正確な JSON Pointer を使い、任意 URL と別サーバーの資格転送を拒否する。"""
    value = _point(value, pointer)
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Download reference is not a string")
    parsed, base = urlsplit(value), urlsplit(endpoint)
    if parsed.scheme or parsed.netloc:
        if (
            (
                parsed.scheme,
                parsed.hostname,
                parsed.port
                if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80),
            )
            != (
                base.scheme,
                base.hostname,
                base.port if base.port is not None else (443 if base.scheme == "https" else 80),
            )
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Download reference changes origin")
        value = parsed.path
    # 原参照の UTF-8 filename を一度だけ復元し、HTTP client の共通処理で再符号化する。
    # 区切りの符号化は path の構造を変えるため拒否し、残る % は二重 decode を防ぐ。
    if re.search(r"%(?:2f|5c)", value, re.IGNORECASE):
        raise ValueError("Download reference contains an encoded path separator")
    return validate_path(unquote(value, errors="strict"))


class McpDownloadProvider:
    """読取 permission、原 binding と取得済み参照を前後で検証し、画像/ログを file にする。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        source: HttpResourceSource,
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """既存 MCP の credential/binding と、redirect/retry を持たない HTTP client を使う。"""
        self._sessions, self._source = sessions, source
        self._binding = McpReadProvider(
            sessions,
            source=StreamableHttpMcpSource(),
            secret_resolver=secret_resolver,
            capability=CAPABILITY,
        )

    async def _observed(
        self, context: RunToolContext, bound: BoundRunResource, ref: str
    ) -> dict[str, Any]:
        """成功した同 Run/同 binding の MCP 応答だけ復元し、可変 file は原 hash と照合する。"""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Evidence, ToolCall)
                    .join(
                        ToolCall,
                        ToolCall.id == Evidence.tool_call_id,
                    )
                    .where(
                        Evidence.run_id == context.run_id,
                        Evidence.evidence_ref == ref,
                        ToolCall.run_id == context.run_id,
                        ToolCall.status == "SUCCEEDED",
                        ToolCall.capability_version == "mcp.query/v1",
                        ToolCall.provider == "mcp",
                        ToolCall.integration_id == context.tool.integration_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ValueError("Original MCP observation is unavailable")
            evidence, tool = row
            if (
                evidence.evidence_type != "resource"
                or evidence.run_id != context.run_id
                or tool.run_id != context.run_id
                or tool.integration_id != context.tool.integration_id
                or (evidence.metadata_json or {}).get("binding_checksum") != bound.checksum
                or evidence.source_locator.get("tool_name") not in bound.scope.get("tool_names", [])
                or tool_access(
                    bound.integration.config,
                    evidence.source_locator.get("tool_name"),
                )
                != "read"
                or not isinstance(tool.result_json, dict)
                or ref not in tool.result_json.get("evidence_refs", [])
                or tool.error_json is not None
            ):
                raise ValueError("Original MCP observation is outside the binding")
            result = dict(tool.result_json)
            original_hash = evidence.content_hash
            original_call_id = tool.id
        if "file" in result:
            file = result["file"]
            expected_path = f"workspace/resources/{original_call_id}/response.json"
            if file.get("path") != expected_path or file.get("content_hash") != original_hash:
                raise ValueError("Original MCP response file changed")
            raw = await asyncio.to_thread(
                read_file,
                context.workspace.root,
                expected_path,
                max_bytes=1_048_576,
            )
            if "sha256:" + sha256_hex(raw) != original_hash:
                raise ValueError("Original MCP response file changed")
            result = json.loads(raw)
        else:
            result.pop("evidence_refs", None)
            if "sha256:" + sha256_hex(canonical_json(result)) != original_hash:
                raise ValueError("Original MCP response changed")
        if (
            not isinstance(result, dict)
            or result.get("status") != "success"
            or result.get("provider") != "mcp"
        ):
            raise ValueError("Original MCP response is invalid")
        return result

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """取得先は原応答から決定し、URL/資格/任意 header を Agent に入力させない。"""
        require_file_access(context)
        ref, pointer = arguments.get("source_evidence_ref"), arguments.get("pointer")
        if (
            not isinstance(ref, str)
            or re.fullmatch(r"ev_[a-zA-Z0-9_-]{1,60}", ref) is None
            or not isinstance(pointer, str)
        ):
            raise ToolProviderError(
                "invalid_request", "Download observation is invalid", retryable=False
            )
        bound, credential = await self._binding._bound(context)
        try:
            result = await self._observed(context, bound, ref)
            path = observed_path(result, pointer, bound.integration.config["server_url"])
            endpoint = urlsplit(bound.integration.config["server_url"])
            await self._same_bound(context, bound, credential)
            response = await self._source.request(
                {
                    "base_url": f"{endpoint.scheme}://{endpoint.netloc}",
                    "auth_mode": "bearer" if credential else "none",
                },
                {"paths": [path], "methods": ["GET"]},
                {"path": path},
                credential,
            )
            if response.status != 200:
                raise HttpResourceError("download_unavailable")
            if "hash_pointer" in arguments:
                expected = _point(result, arguments["hash_pointer"])
                if (
                    not isinstance(expected, str)
                    or re.fullmatch(
                        r"(?:sha256:)?[a-f0-9]{64}",
                        expected,
                    )
                    is None
                    or expected.removeprefix("sha256:") != sha256_hex(response.body)
                ):
                    raise ValueError("Downloaded bytes differ from the observed checksum")
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            MaterializationError,
            SQLAlchemyError,
            HttpResourceError,
        ) as error:
            await self._same_bound(context, bound, credential)
            raise ToolProviderError(
                "unavailable"
                if isinstance(error, SQLAlchemyError | HttpResourceError)
                else "invalid_request",
                "Original download reference or file is unavailable",
                retryable=False,
            ) from None
        await self._same_bound(context, bound, credential)
        file = await store_response(context, response.body, suffix="bin")
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "mcp",
                "file": file,
                "content_type": response.headers.get("content-type"),
                "source_evidence_ref": ref,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="resource-download",
                    source_uri=f"mcp://integration/{context.tool.integration_id}/downloads",
                    source_locator={"source_evidence_ref": ref, "pointer": pointer, "path": path},
                    content_hash=file["content_hash"],
                    metadata={"file": file},
                ),
            ),
        )

    async def _same_bound(
        self, context: RunToolContext, bound: BoundRunResource, credential: str | None
    ) -> None:
        """撤権・資格更新後に取得した byte を file やモデルへ公開しない。"""
        if await self._binding._bound(context) != (bound, credential):
            raise ToolProviderError(
                "scope_denied", "MCP binding changed during download", retryable=False
            )
