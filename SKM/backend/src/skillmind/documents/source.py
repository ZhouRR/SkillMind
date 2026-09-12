"""文書を data source として読む port と、その database/object storage 実装を提供する。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.content import (
    inspect_document_content,
    read_document_content,
    verify_document_bytes,
)
from skillmind.documents.domain import (
    DocumentContentError,
    DocumentContentInvalidError,
    DocumentNotFoundError,
    StoredDocument,
)
from skillmind.documents.repository import DocumentRepository
from skillmind.documents.snapshot import DocumentSnapshotError, FrozenDocument
from skillmind.storage import BlobReference, FileStorage, FileStorageError, sanitize_object_key
from skillmind.storage.observation import BlobObservation


@dataclass(frozen=True, slots=True)
class ProjectDocumentContent:
    """Provider が data source として読む文書の metadata と本文 (blob 正文込み)。"""

    document_id: UUID
    folder: str
    name: str
    mime: str
    checksum: str
    size: int
    data: bytes
    observation: BlobObservation | None = None
    source_object_key: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectDocumentObservation:
    """本文未取得の観測を元 Project/文書 metadata と結び付ける内部参照。"""

    project_id: UUID
    document: FrozenDocument
    observation: BlobObservation
    reference_checksum: str
    source_object_key: str | None = None


@runtime_checkable
class InspectableProjectDocumentSource(Protocol):
    """選択用 metadata のみの観測と、元観測に固定した取得を提供する port。"""

    async def inspect(
        self, *, project_id: UUID, document_id: UUID
    ) -> ProjectDocumentObservation | None:
        """同じ Project の原 ID を観測し、本文や変換結果を取得しない。"""

        ...

    async def fetch_observed(
        self, *, project_id: UUID, observed: ProjectDocumentObservation
    ) -> ProjectDocumentContent | None:
        """元観測に固定した byte を取得し、現在の別文書へ差し替えない。"""

        ...


class ProjectDocumentSource(Protocol):
    """Project と不変 ID で文書を解決する read-only port。"""

    async def fetch(self, *, project_id: UUID, document_id: UUID) -> ProjectDocumentContent | None:
        """一致する文書の内容を返す。存在しなければ None (越権も同様に None)。"""

        ...


class ProjectDocumentInventory(Protocol):
    """Run に凍結済みの文書だけを内容付きで取得する port。"""

    async def list_contents(
        self, *, project_id: UUID, documents: Sequence[FrozenDocument]
    ) -> Sequence[ProjectDocumentContent]:
        """選択済み ID を安定順で取得し、欠落や置換を黙って省略しない。"""

        ...


class DatabaseProjectDocumentInventory:
    """Project を再列挙せず、凍結した ID/hash を共通 source で検証する。"""

    def __init__(
        self,
        *,
        source: ProjectDocumentSource,
    ) -> None:
        """本文解決用 source を保持し、別の資源認可経路を作らない。"""

        self._source = source

    async def list_contents(
        self, *, project_id: UUID, documents: Sequence[FrozenDocument]
    ) -> Sequence[ProjectDocumentContent]:
        """一件でも削除・改変・欠損していれば明示的に失敗させる。"""

        return [
            await read_frozen_document(self._source, project_id=project_id, document=document)
            for document in documents
        ]


class DatabaseProjectDocumentSource:
    """DocumentRepository と FileStorage を用いて文書内容を読み出す本番 source。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        file_storage: FileStorage,
    ) -> None:
        """Database factory と object storage backend を保持する。"""

        self._session_factory = session_factory
        self._file_storage = file_storage

    async def fetch(self, *, project_id: UUID, document_id: UUID) -> ProjectDocumentContent | None:
        """Project と ID の一致を確認して blob を読み、同名の別文書へ切り替えない。"""

        return await self._fetch(project_id=project_id, document_id=document_id)

    async def inspect(
        self, *, project_id: UUID, document_id: UUID
    ) -> ProjectDocumentObservation | None:
        """DB の所在を原 namespace で HEAD し、本文取得済みとは扱わない。"""

        resolved = await self._resolve(project_id=project_id, document_id=document_id)
        if resolved is None:
            return None
        document, reference = resolved
        observation = await inspect_document_content(
            self._file_storage, reference=reference, document=document
        )
        return ProjectDocumentObservation(
            project_id, _frozen_metadata(document), observation, _reference_checksum(reference),
            validate_source_object_key(reference.key),
        )

    async def fetch_observed(
        self, *, project_id: UUID, observed: ProjectDocumentObservation
    ) -> ProjectDocumentContent | None:
        """元 Project・ID・metadata と storage 観測を再確認して同じ内容だけを取得する。"""

        if observed.project_id != project_id:
            raise DocumentSnapshotError("Document observation does not belong to this project")
        return await self._fetch(
            project_id=project_id, document_id=observed.document.document_id, observed=observed
        )

    async def _resolve(
        self, *, project_id: UUID, document_id: UUID
    ) -> tuple[StoredDocument, BlobReference] | None:
        """三つの取得経路で原 Project/ID 解決を共有し、DB session 内で I/O しない。"""

        async with self._session_factory() as session:
            try:
                return await DocumentRepository(session).get_for_download(
                    project_id=project_id, document_id=document_id
                )
            except DocumentNotFoundError:
                return None

    async def _fetch(
        self, *, project_id: UUID, document_id: UUID,
        observed: ProjectDocumentObservation | None = None,
    ) -> ProjectDocumentContent | None:
        """原所在と byte/hash 検証を共有し、前の観測があれば現在版に読み替えない。"""

        resolved = await self._resolve(project_id=project_id, document_id=document_id)
        if resolved is None:
            return None
        document, reference = resolved
        if observed is not None and (
            _frozen_metadata(document) != observed.document
            or _reference_checksum(reference) != observed.reference_checksum
            or (observed.source_object_key is not None
                and observed.source_object_key != reference.key)
        ):
            raise DocumentSnapshotError("Document no longer matches its observed metadata")
        data, observation = await read_document_content(
            self._file_storage, reference=reference, document=document,
            expected=observed.observation if observed is not None else None,
        )
        return ProjectDocumentContent(
            document_id=document.document_id,
            folder=document.folder,
            name=document.name,
            mime=document.mime,
            checksum=document.checksum,
            size=document.size,
            data=data,
            observation=observation,
            source_object_key=validate_source_object_key(reference.key),
        )


def _frozen_metadata(document: StoredDocument) -> FrozenDocument:
    """本文 hash を推測せず、DB の原文書 metadata を比較可能な同じ型へ投影する。"""

    return FrozenDocument(
        document.document_id, document.folder, document.name, document.mime,
        document.size, document.checksum,
    )


def _reference_checksum(reference: BlobReference) -> str:
    """元所在と世代を摘要へ束縛する。公開 key があってもこの原摘要を省略しない。"""

    namespace = reference.namespace
    if namespace is None:
        raise DocumentSnapshotError("Document storage reference is unavailable")
    return f"sha256:{sha256_hex(canonical_json({
        'version': 'v1', 'key': reference.key,
        'namespace_id': str(namespace.namespace_id),
        'namespace_checksum': namespace.descriptor_checksum,
        'durable': namespace.durable,
    }))}"


def validate_source_object_key(value: object) -> str:
    """認可済み source の原 key を正規化せず検証し、接続 URL や署名値を受け取らない。"""

    if (not isinstance(value, str) or not 1 <= len(value) <= 512
        or len(value.encode("utf-8")) > 1024 or sanitize_object_key(value) != value):
        raise DocumentSnapshotError("Document source object key is invalid")
    return value


async def read_frozen_document(
    source: ProjectDocumentSource, *, project_id: UUID, document: FrozenDocument
) -> ProjectDocumentContent:
    """Provider と物化器で ID/hash の確認を共有し、接続や資格情報は外へ出さない。"""

    try:
        content = await source.fetch(project_id=project_id, document_id=document.document_id)
    except (FileStorageError, DocumentContentError) as error:
        raise DocumentSnapshotError("Frozen document content is unavailable") from error
    if content is None:
        raise DocumentSnapshotError("Frozen document is no longer available")
    verify_frozen_content(document, content)
    return content


def verify_frozen_content(document: FrozenDocument, content: ProjectDocumentContent) -> None:
    """申告 hash だけを信頼せず、凍結 metadata と実 byte の一致を検証する。"""

    if (
        content.document_id != document.document_id
        or content.folder != document.folder
        or content.name != document.name
        or content.mime != document.mime
        or content.size != document.size
        or content.checksum != document.content_hash
    ):
        raise DocumentSnapshotError("Frozen document content no longer matches its snapshot")
    try:
        verify_document_bytes(content.data, size=document.size, checksum=document.content_hash)
    except DocumentContentInvalidError as error:
        raise DocumentSnapshotError(
            "Frozen document content no longer matches its snapshot"
        ) from error
