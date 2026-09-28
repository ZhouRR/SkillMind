"""Git の許可 file を編集可能な Run directory に置き、実 byte から提案を作る。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.materialization_storage import (
    MaterializationError,
    read_file,
    write_workspace_file,
)
from skillmind.agent.repository_client import RepositoryClientError
from skillmind.agent.repository_source import RepositoryBindingRef, RepositorySnapshotSource
from skillmind.agent.resource_files import require_file_access, store_response
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.repository_write import _valid_file_path
from skillmind.integrations.domain import path_within_scope


class RepositoryWorkspaceProvider:
    """共有 Git client を使い、shell/credential/.git の権限を model に渡さない。"""

    def __init__(self, source: RepositorySnapshotSource) -> None:
        """read/write/物化と同じ scoped source を保持する。"""
        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """checkout と prepare_commit は remote write を行わない。"""
        require_file_access(context)
        if (
            context.tool.provider != "git"
            or context.tool.integration_id is None
            or context.tool.binding_id is None
        ):
            raise ToolProviderError(
                "invalid_request", "Git workspace requires a frozen binding", retryable=False
            )
        revision = arguments.get("revision")
        if not isinstance(revision, str):
            raise ToolProviderError("invalid_request", "Git revision is required", retryable=False)
        binding = RepositoryBindingRef(
            provider="git",
            integration_id=context.tool.integration_id,
            binding_id=context.tool.binding_id,
        )
        data: dict[str, Any]
        try:
            async with self._source.open(
                project_id=context.project_id,
                run_id=context.run_id,
                binding=binding,
                requested_revision=revision,
            ) as session:
                root = f"workspace/repositories/{context.tool.binding_id}/{session.revision}"
                action = arguments["action"]
                if action == "checkout":
                    paths = arguments.get("paths")
                    listing = await session.list_files(paths)
                    if (
                        len(listing.entries) > 500
                        or sum(item.size for item in listing.entries) > 50 * 1024 * 1024
                    ):
                        raise ValueError(
                            "Checkout exceeds the workspace limit; select a narrower directory"
                        )
                    files = []
                    for entry in listing.entries:
                        if not _valid_file_path(entry.path):
                            raise ValueError("Repository file path is invalid")
                        raw = await session.read_file(entry.path, max_bytes=1_048_576)
                        raw.decode("utf-8")
                        path = root + "/" + entry.path
                        await asyncio.to_thread(
                            write_workspace_file,
                            context.workspace.root,
                            path,
                            raw,
                            expected_hash="absent",
                            reuse_identical=True,
                        )
                        files.append(
                            {
                                "path": entry.path,
                                "local_path": path,
                                "content_hash": "sha256:" + sha256_hex(raw),
                                "size_bytes": len(raw),
                            }
                        )
                    data = {
                        "root": root,
                        "revision": session.revision,
                        "files": files,
                        "skipped": [
                            {"path": item.path, "reason": item.reason} for item in listing.skipped
                        ],
                    }
                elif action == "prepare_commit":
                    changes = []
                    for item in arguments["files"]:
                        path = item["path"]
                        if not _valid_file_path(path) or not path_within_scope(
                            path, session.scope_paths
                        ):
                            raise ValueError("Commit path exceeds the frozen Git scope")
                        if item.get("action") == "REMOVE":
                            changes.append(
                                {"path": "/files/" + path, "action": "REMOVE", "value": None}
                            )
                        else:
                            local = item["source_path"]
                            if not local.startswith(("workspace/", "output/")):
                                raise ValueError("Commit source must be a Run working file")
                            raw = await asyncio.to_thread(
                                read_file, context.workspace.root, local, max_bytes=1_048_576
                            )
                            if "sha256:" + sha256_hex(raw) != item["expected_hash"]:
                                raise ValueError("Commit source changed")
                            changes.append(
                                {
                                    "path": "/files/" + path,
                                    "action": "SET",
                                    "value": raw.decode("utf-8"),
                                }
                            )
                    data = {
                        "resource_key": context.tool.resource_key,
                        "capability_version": "repository.write/v1",
                        "operation": "commit",
                        "target": {"locator": arguments["branch"], "display": arguments["branch"]},
                        "changes": changes,
                        "precondition": {"revision": session.revision},
                        "summary": arguments["summary"],
                        "evidence_refs": [],
                    }
                    if len(canonical_json(data).encode("utf-8")) > 4_194_304:
                        raise ValueError("Commit exceeds the existing proposal limit")
                else:
                    raise ValueError("Unknown Git workspace action")
                resolved_revision = session.revision
        except RepositoryClientError as error:
            raise ToolProviderError(error.code, error.message, retryable=error.retryable) from None
        except (ValueError, MaterializationError, KeyError):
            raise ToolProviderError(
                "invalid_request",
                "Git workspace request is invalid, changed or exceeds scope",
                retryable=False,
            ) from None
        file = await store_response(context, canonical_json(data).encode("utf-8"))
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "git",
                "action": action,
                "revision": resolved_revision,
                "root": root,
                "file": file,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="source_code",
                    source_uri=f"git://integration/{context.tool.integration_id}/{resolved_revision}",
                    source_locator={"revision": resolved_revision, "action": action},
                    content_hash=file["content_hash"],
                    metadata={"file": file, "remote_write": False},
                ),
            ),
        )
