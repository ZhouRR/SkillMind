"""Project 文書を document.read/v1 data source として投影する Provider を実装する。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from skillmind.agent.binary_text import (
    MAX_EXCEL_INPUT_BYTES,
    BinaryTextError,
    convert_excel_to_markdown,
)
from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.text_window import select_line_window
from skillmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from skillmind.artifacts.conversion import conversion_artifact, conversion_artifact_description
from skillmind.core.hashing import sha256_hex
from skillmind.documents.domain import DocumentContentError
from skillmind.documents.observation_repository import (
    DocumentObservationLookup,
    valid_observation_reference,
)
from skillmind.documents.snapshot import (
    DOCUMENT_CONVERT_CAPABILITY,
    DocumentSnapshotError,
    FrozenDocument,
    selected_document_snapshots,
    snapshot_documents,
)
from skillmind.documents.source import (
    InspectableProjectDocumentSource,
    ProjectDocumentContent,
    ProjectDocumentSource,
    read_frozen_document,
    validate_source_object_key,
    verify_frozen_content,
)
from skillmind.storage import FileStorageError


class DocumentProvider:
    """Project 作用域の文書を UTF-8 text として読む read-only Provider。"""

    def __init__(self, source: ProjectDocumentSource) -> None:
        """文書内容を解決する project 作用域 source を保持する。"""

        self._source = source

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Run の project 内で path に一致する文書を、行範囲と content hash 付きで返す。"""

        content = await _load_frozen_content(self._source, context, arguments)
        try:
            text = content.data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolProviderError(
                "invalid_request", "Document is not UTF-8 text", retryable=False
            ) from error
        # 空文書も既定範囲で読めるという document.read/v1 の既存契約を保つ。
        window = select_line_window(text, arguments, subject="Document", allow_empty=True)
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "project",
                "document": {
                    "folder": content.folder,
                    "name": content.name,
                    "mime": content.mime,
                    "checksum": content.checksum,
                    "size": content.size,
                },
                "content": window.content,
                "line_start": window.line_start,
                "line_end": window.line_end,
                "warnings": ["Content was truncated"] if window.truncated else [],
                "truncated": window.truncated,
            },
            evidence=(
                EvidenceDraft(
                    # content hash で addressing し、被参照文書を不変に固定する。
                    evidence_type="document",
                    source_uri=f"document://projects/{context.project_id}/{content.checksum}",
                    source_locator={
                        "document_id": str(content.document_id),
                        "folder": content.folder,
                        "name": content.name,
                        "line_start": window.line_start,
                        "line_end": window.line_end,
                    },
                    content_hash=content.checksum,
                    excerpt=window.content[:2_000],
                    metadata=_source_metadata(content),
                ),
            ),
        )


def _split_document_path(value: Any) -> tuple[str, str]:
    """引数 path を安全化し、(folder, name) の相対 POSIX 分割に落とす。"""

    if not isinstance(value, str) or not 1 <= len(value) <= 512 or "\\" in value:
        raise ToolProviderError("invalid_request", "Document path is invalid", retryable=False)
    parts = value.split("/")
    for part in parts:
        if part in {"", ".", ".."} or not part.isprintable():
            raise ToolProviderError("invalid_request", "Document path is invalid", retryable=False)
    *folder_parts, name = parts
    return "/".join(folder_parts), name


class DocumentConvertProvider:
    """選択済み Excel の原 bytes を明示的に Markdown へ変換する Provider。"""

    def __init__(
        self, source: ProjectDocumentSource, *,
        observations: DocumentObservationLookup | None = None,
    ) -> None:
        """通常読取と同じ Project 文書 source を保持する。"""

        self._source = source
        self._observations = observations

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """変換権と原本を検証し、全 Markdown と二つの hash を Evidence に固定する。"""

        if (
            context.run is None
            or context.tool.capability != DOCUMENT_CONVERT_CAPABILITY
            or DOCUMENT_CONVERT_CAPABILITY not in context.run.permission_snapshot.get(
                "allowed_capabilities", []
            )
        ):
            raise ToolProviderError(
                "scope_denied", "Excel conversion is not allowed", retryable=False
            )
        observation_ref = arguments.get("observation_ref")
        publish_artifact = arguments.get("publish_artifact", False)
        if type(publish_artifact) is not bool:
            raise ToolProviderError(
                "invalid_request", "Artifact publication flag must be boolean", retryable=False,
            )
        if "observation_ref" in arguments:
            if not valid_observation_reference(observation_ref):
                raise ToolProviderError(
                    "invalid_request", "Observation reference is invalid", retryable=False
                )
            assert isinstance(observation_ref, str)
            content = await _load_observed_content(
                self._source, self._observations, context, arguments, observation_ref
            )
        else:
            content = await _load_frozen_content(
                self._source, context, arguments, max_bytes=MAX_EXCEL_INPUT_BYTES
            )
        try:
            converted = await convert_excel_to_markdown(content.name, content.data)
        except BinaryTextError as error:
            raise ToolProviderError("unavailable", str(error), retryable=False) from error
        markdown_hash = f"sha256:{sha256_hex(converted.markdown.encode('utf-8'))}"
        converter = {"name": "markitdown", "version": converted.converter_version}
        result = ProviderToolResult(
            response={
                "status": "success",
                "provider": "project",
                "document": {
                    "document_id": str(content.document_id),
                    "folder": content.folder, "name": content.name,
                    "mime": content.mime, "checksum": content.checksum, "size": content.size,
                },
                "converter": converter,
                "markdown": converted.markdown,
                "markdown_checksum": markdown_hash,
                **({"source_object_key": validate_source_object_key(content.source_object_key)}
                   if content.source_object_key is not None else {}),
                "warnings": [
                    "Conversion may omit formatting, drawings, merged-cell structure and formulas. "
                    "Review conversion loss separately; do not infer original cell coordinates."
                ],
            },
            evidence=(EvidenceDraft(
                evidence_type="document",
                source_uri=f"document://projects/{context.project_id}/{content.checksum}",
                source_locator={
                    "document_id": str(content.document_id),
                    "folder": content.folder, "name": content.name,
                },
                content_hash=content.checksum,
                excerpt=converted.markdown[:2_000],
                metadata={
                    **_source_metadata(content), "converter": converter,
                    "markdown_checksum": markdown_hash,
                    **({"observation_ref": observation_ref} if observation_ref is not None else {}),
                },
            ),),
        )
        if not publish_artifact:
            return result
        artifact, fields = conversion_artifact(result.response, run_id=context.run_id)
        return ProviderToolResult(
            response={**result.response, "artifact": conversion_artifact_description(artifact)},
            evidence=(*result.evidence, EvidenceDraft(**fields, artifact=artifact)),
        )


async def _load_observed_content(
    source: ProjectDocumentSource, observations: DocumentObservationLookup | None,
    context: RunToolContext, arguments: Mapping[str, Any], reference: str,
) -> ProjectDocumentContent:
    """確定した同一 Run の観測へ固定し、欠落時に通常取得へ降格しない。"""

    document = resolve_frozen_document(context, arguments)
    if document.size > MAX_EXCEL_INPUT_BYTES:
        raise ToolProviderError(
            "invalid_request", "Excel input exceeds the conversion limit", retryable=False
        )
    if observations is None or not isinstance(source, InspectableProjectDocumentSource):
        raise ToolProviderError(
            "unavailable", "Stored document observation is unavailable", retryable=False
        )
    try:
        async with asyncio.timeout(20):
            observed = await observations.load(
                project_id=context.project_id, run_id=context.run_id,
                document=document, reference=reference,
            )
            if (
                observed is None or observed.project_id != context.project_id
                or observed.document != document
                or resolve_frozen_document(context, arguments) != document
            ):
                raise DocumentSnapshotError("Stored document observation is unavailable")
            content = await source.fetch_observed(project_id=context.project_id, observed=observed)
            if (content is None or content.observation != observed.observation
                or (observed.source_object_key is not None
                    and content.source_object_key != observed.source_object_key)):
                raise DocumentSnapshotError("Document no longer matches its observation")
            verify_frozen_content(document, content)
            if resolve_frozen_document(context, arguments) != document:
                raise DocumentSnapshotError("Frozen document selection changed")
            return content
    except (DocumentSnapshotError, DocumentContentError, FileStorageError, TimeoutError) as error:
        raise ToolProviderError(
            "unavailable", "Observed document content is unavailable", retryable=False
        ) from error


def _source_metadata(content: ProjectDocumentContent) -> dict[str, Any]:
    """取得時の storage 事実だけを残し、非対応 source の時刻や version を補わない。"""

    metadata: dict[str, Any] = {"reproducibility": "content_hash"}
    if content.observation is not None:
        metadata["storage_observation"] = content.observation.to_json()
    if content.source_object_key is not None:
        metadata["source_object_key"] = validate_source_object_key(content.source_object_key)
    return metadata


async def _load_frozen_content(
    source: ProjectDocumentSource, context: RunToolContext, arguments: Mapping[str, Any],
    *, max_bytes: int | None = None,
) -> ProjectDocumentContent:
    """読取・変換で同じ Run identity と凍結集合・元 bytes の検証を使う。"""

    document = resolve_frozen_document(context, arguments)
    if max_bytes is not None and document.size > max_bytes:
        raise ToolProviderError(
            "invalid_request", "Excel input exceeds the conversion limit", retryable=False
        )
    try:
        content = await read_frozen_document(
            source, project_id=context.project_id, document=document
        )
    except DocumentSnapshotError as error:
        raise ToolProviderError("unavailable", str(error), retryable=False) from error
    return content


def resolve_frozen_document(
    context: RunToolContext, arguments: Mapping[str, Any]
) -> FrozenDocument:
    """観測・読取・変換で同じ Run identity と相対 path 完全一致による範囲を使う。"""

    folder, name = _split_document_path(arguments.get("path"))
    documents = resolve_frozen_documents(context)
    document = next(
        (item for item in documents if item.folder == folder and item.name == name), None
    )
    if document is None:
        raise ToolProviderError("not_found", "Document was not found", retryable=False)
    return document


def resolve_frozen_documents(context: RunToolContext) -> tuple[FrozenDocument, ...]:
    """単一取得と一覧で同じ Run identity と凍結した選択和集合だけを解決する。"""

    if context.run is None or (
        context.run.run_id != context.run_id
        or context.run.project_id != context.project_id
        or context.run.run_attempt_id != context.run_attempt_id
        or context.run.user_id != context.user_id
    ):
        raise ToolProviderError(
            "scope_denied", "Frozen Run context is required", retryable=False
        )
    try:
        snapshots = selected_document_snapshots(
            context.run.resolved_sources, project_id=context.project_id
        )
        documents = snapshot_documents(snapshots)
    except DocumentSnapshotError as error:
        raise ToolProviderError("scope_denied", str(error), retryable=False) from error
    return documents
