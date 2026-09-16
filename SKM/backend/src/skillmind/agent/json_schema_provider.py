"""Run 内の二つの JSON file を read-only で照合し、hash と検証 Evidence を返す。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.json_schema_validation import MAX_FILE_BYTES
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.workspace_provider import (
    _read_workspace_bytes,
    _resolve_workspace_path,
    _resolve_writable_path,
    _workspace_uri,
)
from skillmind.core.hashing import canonical_json, sha256_hex

_ERRORS = {
    "invalid_json",
    "invalid_schema",
    "external_reference",
    "unsupported_schema",
    "unsupported_format",
    "too_large",
    "validation_limit",
}
_TIMEOUT_SECONDS = 8


class JsonSchemaValidateProvider:
    """許可された Run input/workspace/output だけを検証する unbound workspace Tool。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """既存の安全読取と input seal を再利用し、検証完了前に valid を返さない。"""
        files: list[tuple[str, bytes]] = []
        for key in ("schema_path", "instance_path"):
            path = arguments.get(key)
            if isinstance(path, str) and path.startswith("output/"):
                relative, _, target = await asyncio.to_thread(_resolve_writable_path, context, path)
            else:
                relative, target = await asyncio.to_thread(
                    _resolve_workspace_path, context, path, require_file=True
                )
            data = await asyncio.to_thread(
                _read_workspace_bytes, context, relative, target, max_bytes=MAX_FILE_BYTES
            )
            files.append((relative, data))
        try:
            payload = json.dumps(
                {"schema": files[0][1].decode("utf-8"), "instance": files[1][1].decode("utf-8")},
                ensure_ascii=False,
            ).encode("utf-8")
        except UnicodeError as error:
            raise ToolProviderError(
                "invalid_json", "Files must be UTF-8 JSON", retryable=False
            ) from error
        result = await _validate_in_process(payload)
        hashes = [f"sha256:{sha256_hex(data)}" for _, data in files]
        response = {
            "status": "success",
            "provider": "workspace",
            **result,
            "schema_path": files[0][0],
            "instance_path": files[1][0],
            "schema_hash": hashes[0],
            "instance_hash": hashes[1],
        }
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="json-schema-validation",
                    source_uri=_workspace_uri(context, files[1][0]),
                    source_locator={"schema_path": files[0][0], "instance_path": files[1][0]},
                    content_hash=f"sha256:{sha256_hex(canonical_json(response))}",
                    metadata={
                        "schema_hash": hashes[0],
                        "instance_hash": hashes[1],
                        "valid": result["valid"],
                        "read_only": True,
                    },
                ),
            ),
        )


async def _validate_in_process(payload: bytes) -> dict[str, Any]:
    """固定 Python module だけを起動し、timeout/cancel 時も子を回収してから戻る。"""
    environment = {
        key: os.environ[key] for key in ("PATH", "PYTHONPATH", "SYSTEMROOT") if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "skillmind.agent.json_schema_validation",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=environment,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(payload), _TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ToolProviderError(
            "validation_limit", "JSON validation exceeded its limit", retryable=False
        ) from error
    if process.returncode != 0 or len(stdout) > 524_288:
        raise ToolProviderError(
            "validation_limit", "JSON validation did not complete", retryable=False
        )
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("Invalid validator response")
        if "error" in result:
            code = result["error"] if result["error"] in _ERRORS else "invalid_schema"
            raise ToolProviderError(code, f"JSON validation rejected: {code}", retryable=False)
        if not isinstance(result.get("valid"), bool):
            raise ValueError("Invalid validator response")
        return result
    except (ValueError, TypeError, AttributeError) as error:
        raise ToolProviderError(
            "unavailable", "JSON validation result is unavailable", retryable=False
        ) from error
