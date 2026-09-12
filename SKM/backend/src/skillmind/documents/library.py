"""既存 Project 文書庫の保存先を、外部 Integration を偽造せず Run に固定する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

from minio.helpers import check_bucket_name
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import ResourceBinding
from skillmind.documents.paths import document_effect_prefix
from skillmind.integrations.domain import ResourceBindingLevel, binding_checksum
from skillmind.storage.blob import FileStorage, FileStorageError, StorageNamespace

DOCUMENT_LIBRARY_PROVIDER = "project-library"
DOCUMENT_WRITE_CAPABILITY = "document.write/v1"
DOCUMENT_LIBRARY_REVISION = "1"
DOCUMENT_LIBRARY_SELECTION = "project-library:documents"


@dataclass(frozen=True, slots=True)
class DocumentLibraryTarget:
    """Composition が渡す既存 namespace/bucket。接続 URL・鍵・本文は保持しない。"""

    namespace: StorageNamespace
    bucket: str

    def __post_init__(self) -> None:
        """memory/未所属の保存先と曖昧な bucket を成果保存の対象にしない。"""

        if not isinstance(self.namespace, StorageNamespace) or not self.namespace.durable:
            raise ValueError("Document library requires a durable storage namespace")
        try:
            check_bucket_name(self.bucket, strict=True)
        except (TypeError, ValueError) as error:
            raise ValueError("Document library bucket is invalid") from error

    def scope(self, project_id: UUID) -> dict[str, str]:
        """任意 prefix は受け取らず、同 Project の成果領域だけを共有 path 規則で導出する。"""

        if not isinstance(project_id, UUID) or project_id.int == 0:
            raise ValueError("Document library project is invalid")
        return {
            "project_id": str(project_id),
            "namespace_id": str(self.namespace.namespace_id),
            "descriptor_checksum": self.namespace.descriptor_checksum,
            "bucket": self.bucket,
            "key_prefix": document_effect_prefix(project_id),
        }

    def reference(self, project_id: UUID) -> dict[str, str]:
        """同 namespace/bucket/Project の庫に、Run/slot に依存しない登録用 ID を与える。

        文書庫は Project に属する論理資源で、Integration や別の DB row を作らない。
        元 scope の照合後だけ公開し、接続情報や任意の保存権を含めない。
        """

        scope = self.scope(project_id)
        identity = canonical_json({
            "kind": "project-document-library/v1",
            "project_id": scope["project_id"],
            "bucket": self.bucket,
        })
        return {
            "document_library_id": str(uuid5(self.namespace.namespace_id, identity)),
            "project_id": scope["project_id"],
            "bucket": self.bucket,
        }


def configured_document_library(
    storage: FileStorage, *, bucket: str
) -> DocumentLibraryTarget | None:
    """既存 FileStorage の所属が確定している場合だけ、同じ保存先を候補に使う。"""

    namespace = storage.namespace
    if namespace is None or not namespace.durable:
        return None
    return DocumentLibraryTarget(namespace, bucket)


@dataclass(frozen=True, slots=True)
class ResolvedDocumentLibraryBinding:
    """Run ID 確定前の選択。Integration の契約を偽造せず同じ作成 transaction で凍結する。"""

    requirement_key: str
    target: DocumentLibraryTarget

    def source(self, *, project_id: UUID) -> dict[str, Any]:
        """まだ dispatch できない初期選択を返し、作成 transaction 内で元 binding を付加する。"""

        return {
            "capability": DOCUMENT_WRITE_CAPABILITY,
            "provider": DOCUMENT_LIBRARY_PROVIDER,
            "candidate_key": DOCUMENT_LIBRARY_SELECTION,
            "resource_kind": "document",
            "access": "write",
            "revision": DOCUMENT_LIBRARY_REVISION,
            "scope": self.target.scope(project_id),
        }


@dataclass(frozen=True, slots=True)
class FrozenDocumentLibraryBinding:
    """履歴/参照保護でも検証できる保存先 snapshot。現在の接続情報は補填しない。"""

    project_id: UUID
    run_id: UUID
    binding_id: UUID
    requirement_key: str
    target: DocumentLibraryTarget

    def to_json(self) -> dict[str, Any]:
        """原 binding と共通 checksum の完全な表現を返す。正文/Secret/入力清単は持たない。"""

        if (
            any(
                not isinstance(value, UUID) or value.int == 0
                for value in (self.run_id, self.binding_id)
            )
            or not isinstance(self.requirement_key, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", self.requirement_key) is None
        ):
            raise ValueError("Document library snapshot identity is invalid")
        source = ResolvedDocumentLibraryBinding(self.requirement_key, self.target).source(
            project_id=self.project_id
        )
        payload = {
            **source,
            "snapshot_version": "v1",
            "run_id": str(self.run_id),
            "binding_id": str(self.binding_id),
            "binding_capability": DOCUMENT_WRITE_CAPABILITY,
            "binding_checksum": binding_checksum(
                project_id=self.project_id,
                scope_level=ResourceBindingLevel.RUN,
                scope_key=str(self.run_id),
                requirement_key=self.requirement_key,
                resource_kind="document",
                integration_id=None,
                provider=DOCUMENT_LIBRARY_PROVIDER,
                capability_version=DOCUMENT_WRITE_CAPABILITY,
                revision=DOCUMENT_LIBRARY_REVISION,
                scope=source["scope"],
            ),
        }
        return {**payload, "snapshot_checksum": f"sha256:{sha256_hex(canonical_json(payload))}"}


def is_document_library_source(source: object) -> bool:
    """保存先を名乗る破損も捕捉する。True は検証済みや参照不要を意味しない。"""

    return isinstance(source, Mapping) and (
        source.get("provider") == DOCUMENT_LIBRARY_PROVIDER
        or source.get("capability") == DOCUMENT_WRITE_CAPABILITY
        or source.get("candidate_key") == DOCUMENT_LIBRARY_SELECTION
    )


def parse_document_library_source(
    value: Mapping[str, Any], *, project_id: UUID, requirement_key: str, run_id: UUID | None = None
) -> FrozenDocumentLibraryBinding:
    """完全な保存表現と元 Project/slot/Run を照合し、入力文書の偽装除外を拒否する。"""

    try:
        scope_project, target = document_library_scope(value["scope"])
        snapshot = FrozenDocumentLibraryBinding(
            project_id, UUID(value["run_id"]), UUID(value["binding_id"]), requirement_key, target
        )
        if (
            scope_project != project_id
            or (run_id is not None and snapshot.run_id != run_id)
            or canonical_json(dict(value)) != canonical_json(snapshot.to_json())
        ):
            raise ValueError("Document library snapshot does not match its original binding")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError("Document library snapshot is invalid") from error
    return snapshot


def document_library_scope(value: Mapping[str, Any]) -> tuple[UUID, DocumentLibraryTarget]:
    """保存 scope は過不足なく検証し、別 Project/prefix/未知 field を黙って捨てない。"""

    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "project_id",
            "namespace_id",
            "descriptor_checksum",
            "bucket",
            "key_prefix",
        }
        or not all(isinstance(item, str) for item in value.values())
    ):
        raise ValueError("Document library scope is invalid")
    try:
        project_id = UUID(value["project_id"])
        target = DocumentLibraryTarget(
            StorageNamespace(UUID(value["namespace_id"]), value["descriptor_checksum"], True),
            value["bucket"],
        )
    except (ValueError, TypeError, FileStorageError) as error:
        raise ValueError("Document library scope is invalid") from error
    if canonical_json(dict(value)) != canonical_json(target.scope(project_id)):
        raise ValueError("Document library scope does not match its project or storage")
    return project_id, target


class DocumentLibraryBindingRepository:
    """呼出元の Run 作成/原 Effect 認可 transaction で使う、明示的な内部資源 binding。"""

    def __init__(self, session: AsyncSession, *, target: DocumentLibraryTarget) -> None:
        """現在構成された保存先を必須にし、保存 JSON から接続を作り直さない。"""

        self._session = session
        self._target = target

    async def freeze(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        requirement_key: str,
        actor_id: UUID,
    ) -> ResourceBinding:
        """認可済み Run 作成と同一 transaction に、Integration のない成果 binding を保存する。"""

        if any(not isinstance(value, UUID) or value.int == 0 for value in (run_id, actor_id)):
            raise ValueError("Document library binding identity is invalid")
        now = datetime.now(UTC)
        scope = self._target.scope(project_id)
        checksum = binding_checksum(
            project_id=project_id,
            scope_level=ResourceBindingLevel.RUN,
            scope_key=str(run_id),
            requirement_key=requirement_key,
            resource_kind="document",
            integration_id=None,
            provider=DOCUMENT_LIBRARY_PROVIDER,
            capability_version=DOCUMENT_WRITE_CAPABILITY,
            revision=DOCUMENT_LIBRARY_REVISION,
            scope=scope,
        )
        binding = ResourceBinding(
            id=uuid4(),
            project_id=project_id,
            run_id=run_id,
            scope_level=ResourceBindingLevel.RUN.value,
            scope_key=str(run_id),
            requirement_key=requirement_key,
            resource_kind="document",
            integration_id=None,
            source_binding_id=None,
            provider=DOCUMENT_LIBRARY_PROVIDER,
            capability_version=DOCUMENT_WRITE_CAPABILITY,
            revision=DOCUMENT_LIBRARY_REVISION,
            scope_json=scope,
            checksum=checksum,
            created_by=actor_id,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self.validate(
            binding, project_id=project_id, run_id=run_id, requirement_key=requirement_key
        )
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def require(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        binding_id: UUID,
        requirement_key: str,
    ) -> ResourceBinding:
        """同一保存行を新しく読み、元 scope と現在構成の世代を実行前に照合する。"""

        binding: ResourceBinding | None = await self._session.scalar(
            select(ResourceBinding)
            .where(ResourceBinding.id == binding_id)
            .execution_options(populate_existing=True)
        )
        if binding is None:
            raise ValueError("Document library binding is unavailable")
        self.validate(
            binding, project_id=project_id, run_id=run_id, requirement_key=requirement_key
        )
        return binding

    def validate(
        self,
        binding: ResourceBinding,
        *,
        project_id: UUID,
        run_id: UUID,
        requirement_key: str,
    ) -> None:
        """原 binding の帰属・能力・scope/hash・失効と現在の保存先を一括照合する。"""

        if (
            not isinstance(run_id, UUID)
            or run_id.int == 0
            or not isinstance(requirement_key, str)
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", requirement_key) is None
        ):
            raise ValueError("Document library binding identity is invalid")
        expected_scope = self._target.scope(project_id)
        expected_checksum = binding_checksum(
            project_id=project_id,
            scope_level=ResourceBindingLevel.RUN,
            scope_key=str(run_id),
            requirement_key=requirement_key,
            resource_kind="document",
            integration_id=None,
            provider=DOCUMENT_LIBRARY_PROVIDER,
            capability_version=DOCUMENT_WRITE_CAPABILITY,
            revision=DOCUMENT_LIBRARY_REVISION,
            scope=expected_scope,
        )
        if (
            binding.project_id != project_id
            or binding.run_id != run_id
            or binding.scope_level != ResourceBindingLevel.RUN.value
            or binding.scope_key != str(run_id)
            or binding.requirement_key != requirement_key
            or binding.resource_kind != "document"
            or binding.integration_id is not None
            or binding.source_binding_id is not None
            or binding.disabled_at is not None
            or binding.provider != DOCUMENT_LIBRARY_PROVIDER
            or binding.capability_version != DOCUMENT_WRITE_CAPABILITY
            or binding.revision != DOCUMENT_LIBRARY_REVISION
            or canonical_json(binding.scope_json) != canonical_json(expected_scope)
            or binding.checksum != expected_checksum
        ):
            raise ValueError("Document library binding changed after Run creation")
