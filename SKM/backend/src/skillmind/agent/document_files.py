"""文書庫の論理 path を列挙・観測し、原 byte 参照から批准用の要求を準備する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import ProjectDocument
from skillmind.documents.file_state import FOLDER_OPERATIONS, file_description, observe_file_state
from skillmind.documents.library import (
    DocumentLibraryBindingRepository,
    DocumentLibraryTarget,
    parse_document_library_source,
)
from skillmind.documents.management import DocumentManagementRepository
from skillmind.documents.paths import validate_document_path
from skillmind.effects.document_write import document_proposal_payload
from skillmind.storage import UploadRejectedError

CAPABILITY = "document.files/v1"


class DocumentFilesProvider:
    """書込 binding 内の metadata だけを提供する。保存・削除は共有 Effect に渡す。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        target: DocumentLibraryTarget | None = None,
    ) -> None:
        """現在構成と既存 binding の同じ検証入口を受け取る。"""
        self._session_factory, self._target = session_factory, target

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """本文を応答へ含めず、観測 Evidence と承認前の短い要求だけを返す。"""
        if self._session_factory is None or self._target is None:
            raise ToolProviderError(
                "unavailable", "Document library is unavailable", retryable=False
            )
        run, key = context.run, arguments.get("library_key")
        if not isinstance(key, str):
            raise ToolProviderError("invalid_request", "Library key is required", retryable=False)
        if (
            run is None
            or run.run_id != context.run_id
            or run.project_id != context.project_id
            or run.run_attempt_id != context.run_attempt_id
            or run.user_id != context.user_id
            or "document.write/v1" not in run.permission_snapshot.get("allowed_capabilities", [])
        ):
            raise ToolProviderError(
                "scope_denied", "Document library is not authorized", retryable=False
            )
        try:
            binding = parse_document_library_source(
                run.resolved_sources[key],
                project_id=context.project_id,
                requirement_key=key,
                run_id=context.run_id,
            )
            if binding.target != self._target:
                raise ValueError("Library target changed")
            async with self._session_factory() as session:
                current = await DocumentLibraryBindingRepository(
                    session, target=self._target
                ).require(
                    project_id=context.project_id,
                    run_id=context.run_id,
                    binding_id=binding.binding_id,
                    requirement_key=key,
                )
                if current.checksum != binding.to_json()["binding_checksum"]:
                    raise ValueError("Library binding changed")
                body = await self._execute(session, context, arguments)
                # SQL 待機中の binding 失効も、metadata の公開前に確認する。
                confirmed = await DocumentLibraryBindingRepository(
                    session, target=self._target,
                ).require(
                    project_id=context.project_id, run_id=context.run_id,
                    binding_id=binding.binding_id, requirement_key=key,
                )
                if confirmed.checksum != binding.to_json()["binding_checksum"]:
                    raise ValueError("Library binding changed")
        except (ValueError, KeyError, TypeError, LookupError, UploadRejectedError):
            raise ToolProviderError(
                "invalid_request",
                "Library path, revision or file reference is invalid",
                retryable=False,
            ) from None
        response = {"status": "success", "provider": "project-library", "library_key": key, **body}
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="document-files",
                    source_uri=f"project-document://{context.project_id}/files",
                    source_locator={
                        "library_key": key,
                        "action": arguments["action"],
                        "path": arguments.get("path", ""),
                    },
                    content_hash="sha256:" + sha256_hex(canonical_json(body)),
                    metadata={"scope": "authorized-library-metadata", "read_only": True},
                ),
            ),
        )

    async def _execute(
        self,
        session: AsyncSession,
        context: RunToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """一覧は metadata page、保存準備は immutable Artifact を使い、外部変更しない。"""
        assert self._target is not None
        action, path = arguments["action"], arguments.get("path", "")
        if path:
            folder, _, name = path.rpartition("/")
            if validate_document_path(project_id=context.project_id, folder=folder, name=name) != (
                folder,
                name,
            ):
                raise ValueError("Noncanonical path")
        if action == "list":
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 50)
            rows = list(
                await session.scalars(
                    select(ProjectDocument)
                    .where(
                        ProjectDocument.project_id == context.project_id,
                        ProjectDocument.deleted_at.is_not(None)
                        if arguments.get("trashed", False)
                        else ProjectDocument.deleted_at.is_(None),
                    )
                    .order_by(ProjectDocument.folder, ProjectDocument.name, ProjectDocument.id)
                )
            )
            rows = [
                d for d in rows if not path or d.folder == path or d.folder.startswith(path + "/")
            ]
            directories = await DocumentManagementRepository(session).folders(context.project_id)
            entries = [
                {"kind": "directory", "path": p}
                for p in directories
                if not path or p == path or p.startswith(path + "/")
            ] + [{"kind": "file", **file_description(d)} for d in rows]
            entries.sort(key=lambda item: (item["path"], item.get("document_id", "")))
            revision = "sha256:" + sha256_hex(canonical_json(entries))
            if arguments.get("expected_revision", revision) != revision or (
                offset and "expected_revision" not in arguments
            ):
                raise ValueError("Directory changed between pages")
            end = min(offset + limit, len(entries))
            return {
                "listing": {
                    "entries": entries[offset:end],
                    "revision": revision,
                    "next_offset": end if end < len(entries) else None,
                    "total": len(entries),
                }
            }
        operation = arguments.get("operation")
        observed = await observe_file_state(
            session,
            project_id=context.project_id,
            path=path,
            folder=arguments.get("kind") == "directory" or operation in FOLDER_OPERATIONS,
            trashed=arguments.get("trashed", False) or operation == "RESTORE",
            document_id=arguments.get("document_id"),
        )
        if action == "stat":
            return {"state": observed}
        if action != "prepare" or not isinstance(operation, str):
            raise ValueError("Unknown action")
        if operation in {"CREATE", "CREATE_FOLDER"} and (
            observed.get("document") or observed.get("exists")
        ):
            raise ValueError("Target already exists; observe before preparing an update")
        value = {}
        if operation in {"CREATE", "UPDATE"}:
            artifact = await ArtifactRepository(session).get_content(
                project_id=context.project_id,
                run_id=context.run_id,
                artifact_ref=arguments["artifact_ref"],
            )
            if artifact is None:
                raise ValueError("Original Artifact is unavailable")
            value = {
                "artifact_ref": artifact.metadata.artifact_ref,
                "content_hash": artifact.metadata.checksum,
                "size_bytes": artifact.metadata.size_bytes,
                "mime_type": arguments["mime_type"],
            }
        elif operation in {"MOVE", "MOVE_FOLDER"}:
            value = {"destination": arguments["destination"]}
        elif operation == "RESTORE":
            if observed["document"] is None:
                raise ValueError("Original recycled document is unavailable")
            value = {"document_id": observed["document"]["document_id"]}
        revision = (
            "absent" if operation in {"CREATE", "CREATE_FOLDER"} else arguments["expected_revision"]
        )
        if revision != "absent" and observed["revision"] != revision:
            raise ValueError("Original file observation changed")
        proposal = {
            "resource_key": arguments["library_key"],
            "capability_version": "document.write/v1",
            "operation": operation,
            "target": {"locator": path, "display": path[:500]},
            "changes": [{"path": "/document", "action": "SET", "value": value}],
            "precondition": {"revision": revision},
            "summary": arguments["purpose"],
            "verification": {"method": "READ_BACK", "paths": ["/document"]},
        }
        document_proposal_payload(
            operation=operation,
            target=proposal["target"],
            changes=proposal["changes"],
            precondition=proposal["precondition"],
            verification=proposal["verification"],
            scope=self._target.scope(context.project_id),
        )
        return {"state": observed, "proposal": proposal}
