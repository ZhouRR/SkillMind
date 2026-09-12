"""設定から本番 FileStorage を組み立てる composition 用 factory。

MinIO/S3 具象を import するため、consumer(composition root)だけが読み込む。
"""

from __future__ import annotations

from skillmind.core.settings import Settings
from skillmind.storage.blob import FileStorage
from skillmind.storage.s3 import S3FileStorage
from skillmind.storage.s3_effect import S3ObjectWriteSource
from skillmind.storage.validation import UploadLimits


def create_file_storage(settings: Settings) -> FileStorage:
    """object_storage 設定から MinIO/S3 backend を組み立てる。"""

    return S3FileStorage(
        endpoint=settings.object_storage_endpoint,
        bucket=settings.object_storage_bucket,
        access_key=settings.object_storage_access_key,
        secret_key=settings.object_storage_secret_key,
        namespace_id=settings.object_storage_namespace_id,
    )


def create_document_upload_limits(settings: Settings) -> UploadLimits:
    """設定からアップロード上限と許可 content-type を組み立てる。"""

    return UploadLimits(
        max_bytes=settings.document_max_bytes,
        project_quota_bytes=settings.project_document_quota_bytes,
        allowed_content_types=frozenset(settings.document_allowed_content_types),
    )


def create_document_write_source(
    settings: Settings, *, storage: FileStorage
) -> S3ObjectWriteSource:
    """既存文書庫と同じ設定を使い、未所属/別世代の成果 writer は接続前に拒否する。"""

    if settings.object_storage_namespace_id is None:
        raise ValueError("Document writes require the configured storage namespace")
    source = S3ObjectWriteSource(
        endpoint=settings.object_storage_endpoint,
        bucket=settings.object_storage_bucket,
        namespace_id=settings.object_storage_namespace_id,
        access_key=settings.object_storage_access_key,
        secret_key=settings.object_storage_secret_key,
    )
    if source.namespace != storage.namespace:
        raise ValueError("Document writer does not match the configured project library")
    return source
