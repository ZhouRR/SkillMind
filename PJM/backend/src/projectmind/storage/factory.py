"""設定から本番 FileStorage を組み立てる composition 用 factory。

MinIO/S3 具象を import するため、consumer(composition root)だけが読み込む。
"""

from __future__ import annotations

from projectmind.core.settings import Settings
from projectmind.storage.blob import FileStorage
from projectmind.storage.s3 import S3FileStorage
from projectmind.storage.validation import UploadLimits


def create_file_storage(settings: Settings) -> FileStorage:
    """object_storage 設定から MinIO/S3 backend を組み立てる。"""

    return S3FileStorage(
        endpoint=settings.object_storage_endpoint,
        bucket=settings.object_storage_bucket,
        access_key=settings.object_storage_access_key,
        secret_key=settings.object_storage_secret_key,
    )


def create_document_upload_limits(settings: Settings) -> UploadLimits:
    """設定からアップロード上限と許可 content-type を組み立てる。"""

    return UploadLimits(
        max_bytes=settings.document_max_bytes,
        project_quota_bytes=settings.project_document_quota_bytes,
        allowed_content_types=frozenset(settings.document_allowed_content_types),
    )
