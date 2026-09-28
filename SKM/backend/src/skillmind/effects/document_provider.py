"""文書庫の保存・目录操作を原 ledger の一回送信と回执照会へ接続する approved-effect Provider。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from skillmind.documents.domain import (
    DocumentConflictError,
    DocumentInUseError,
    DocumentNotFoundError,
    StoredDocument,
)
from skillmind.documents.file_state import FILE_OPERATIONS
from skillmind.documents.library import (
    DOCUMENT_LIBRARY_PROVIDER,
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryTarget,
)
from skillmind.effects.document_service import DocumentEffectService
from skillmind.effects.document_write import DOCUMENT_WRITE_PROVIDER_VERSION
from skillmind.effects.domain import (
    ChangeProposalValidationError,
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectLeaseValidationError,
    EffectProviderResult,
)
from skillmind.effects.redmine import EffectProviderStaleError, EffectProviderTransportError
from skillmind.storage.effect_write import (
    ObjectWriteCommand,
    ObjectWriteConflictError,
    ObjectWriteReceipt,
    ObjectWriteUncertainError,
)
from skillmind.storage.s3_effect import S3ObjectWriteSource
from skillmind.storage.validation import UploadRejectedError


class DocumentWriteProvider:
    """Artifact byte/保存先を固定し、送信前の commit 確認と原結果だけで再開する。"""

    def __init__(self, *, service: DocumentEffectService, source: S3ObjectWriteSource) -> None:
        """認可/ledger transaction と実ストレージ I/O を別 port として受け取る。"""

        self._service = service
        self._source = source

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """初回は一度の PUT、SENT は GET、核対済みは保存済み回执からだけ再開する。"""

        frozen = deepcopy(execution)
        if (
            frozen.capability_version != DOCUMENT_WRITE_CAPABILITY
            or frozen.provider != DOCUMENT_LIBRARY_PROVIDER
            or credential is not None
            or frozen.integration_id is not None
        ):
            raise ValueError("Document library effect has no Integration credential")
        try:
            if frozen.operation in FILE_OPERATIONS:
                return await self._service.apply_management(frozen)
            prepared = await self._service.prepare(frozen)
            command, receipt = prepared.command, prepared.receipt
            if command.namespace != self._source.namespace:
                raise ValueError("Document storage source does not match its approved target")

            async def authorize() -> None:
                """各 I/O 段階で原批准/lease を再検査し、DB lock をネットワークへ持ち越さない。"""

                await self._service.authorize(frozen)

            replayed = True
            if receipt is None:
                if await self._service.start_once(frozen, command):
                    replayed = False
                    receipt = await self._source.create_once(command, authorize=authorize)
                else:
                    receipt = await self._source.lookup(command, authorize=authorize)
                    if receipt is None:
                        # 旧 PUT の未到達/停止は一回の GET で証明できない。新 ID/再 PUT は禁止。
                        raise ObjectWriteUncertainError("Original document write requires lookup")
            document = await self._service.publish(frozen, command, receipt)
        except (
            PermissionError,
            EffectLeaseValidationError,
            ChangeProposalValidationError,
        ) as error:
            raise EffectProviderTransportError(
                "effect_authority_revoked", retryable=False
            ) from error
        except (
            DocumentConflictError,
            DocumentInUseError,
            DocumentNotFoundError,
            ObjectWriteConflictError,
        ) as error:
            # UPDATE の確定競合だけ公開を閉じる。結果未知はこの分岐へ流さない。
            if frozen.operation == "UPDATE":
                try:
                    await self._service.close_conflicted_replacement(frozen)
                except Exception as closure_error:
                    raise EffectProviderTransportError(
                        "document_effect_uncertain", retryable=True
                    ) from closure_error
            # 原 object/SENT/配額は保持し、別 Effect の新規保存を妨げる path 占用だけ解放する。
            raise EffectProviderStaleError("Original document target conflicts") from error
        except UploadRejectedError as error:
            raise EffectProviderTransportError(error.code, retryable=False) from error
        except ValueError:
            raise
        except Exception as error:
            # commit/PUT/回读/公開の応答未知を同じ原 ledger で核対する。例外本文は公開しない。
            # CancelledError は捕捉せず、永続 SENT を残して Worker の回収に委ねる。
            raise EffectProviderTransportError(
                "document_effect_uncertain", retryable=True
            ) from error
        return _result(
            command, receipt, document, replayed=replayed, precondition=frozen.precondition
        )


def _result(
    command: ObjectWriteCommand,
    receipt: ObjectWriteReceipt,
    document: StoredDocument,
    *,
    replayed: bool,
    precondition: Mapping[str, Any] | None = None,
) -> EffectProviderResult:
    """原回执と公開 metadata を証拠化し、現在の存在や未観測の事前状態を断定しない。"""

    locator = {
        "document_id": str(document.document_id),
        "path": f"{document.folder}/{document.name}" if document.folder else document.name,
        "effect_id": str(command.effect_id),
    }
    metadata = {
        "provider": DOCUMENT_LIBRARY_PROVIDER,
        "provider_version": DOCUMENT_WRITE_PROVIDER_VERSION,
        "request_checksum": command.request_checksum,
    }
    uri = f"project-document://{document.project_id}/{document.document_id}"
    return EffectProviderResult(
        before=EffectEvidenceDraft(
            evidence_type="document",
            source_uri=uri,
            source_locator={**locator, "phase": "approved_precondition"},
            content={"precondition": precondition or {"revision": "absent"}},
            excerpt=None,
            metadata={**metadata, "observed_remote_absence": False},
        ),
        after=EffectEvidenceDraft(
            evidence_type="document",
            source_uri=uri,
            source_locator={**locator, "phase": "published"},
            content={
                "document": {
                    "document_id": str(document.document_id),
                    "path": locator["path"],
                    "storage": {
                        "document_library_id": DocumentLibraryTarget(
                            command.namespace,
                            command.bucket,
                        ).reference(command.project_id)["document_library_id"],
                        "bucket": command.bucket,
                        "object_key": command.object_key,
                        "version_id": receipt.version_id,
                        "etag": receipt.etag,
                    },
                    "content_hash": document.checksum,
                    "size_bytes": document.size,
                    "mime_type": document.mime,
                    "published_at": document.created_at.isoformat(),
                }
            },
            excerpt=None,
            metadata={**metadata, "original_object_receipt": True},
        ),
        verification={
            "method": "READ_BACK",
            "matched_paths": ["/document"],
            "effect_id": str(command.effect_id),
            "document_id": str(document.document_id),
            "request_checksum": receipt.request_checksum,
            "content_hash": receipt.content_checksum,
            "replayed": replayed,
        },
        replayed=replayed,
    )
