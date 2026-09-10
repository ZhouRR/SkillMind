"""受信と保存済み原要求で同じ文書 path/key 規則を用いる。"""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID

from projectmind.storage import FileStorageError, UploadRejectedError, sanitize_object_key


def validate_document_path(*, project_id: UUID, folder: str, name: str) -> tuple[str, str]:
    """表示 path を正規化し、storage 固有の単段上限も PUT 前に検査する。"""

    safe_folder, safe_name = _safe_folder(folder), _safe_name(name)
    document_storage_key(project_id, UUID(int=0), safe_name)
    return safe_folder, safe_name


def document_storage_key(project_id: UUID, document_id: UUID, name: str) -> str:
    """共有 storage 規則の拒否を公開文書名の安定した拒否へ変換する。"""

    try:
        return sanitize_object_key(f"projects/{project_id}/documents/{document_id}/{name}")
    except FileStorageError as error:
        raise UploadRejectedError(
            "invalid_document_name", "Document name cannot be stored safely"
        ) from error


def _safe_name(value: str) -> str:
    """文書名を単一の安全な file 名に限定し、切り詰めによる衝突を作らない。"""

    candidate = value.strip()
    if not candidate or len(candidate) > 200:
        raise UploadRejectedError("invalid_document_name", "Document name is empty or too long")
    if candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
        raise UploadRejectedError("invalid_document_name", "Document name must be a single segment")
    if not candidate.isprintable():
        raise UploadRejectedError(
            "invalid_document_name", "Document name has unprintable characters"
        )
    return candidate


def _safe_folder(value: str) -> str:
    """Folder を空の root または安全な相対 POSIX path に限定する。"""

    candidate = value.strip().strip("/")
    if not candidate:
        return ""
    if len(candidate) > 200 or "\\" in candidate:
        raise UploadRejectedError("invalid_document_folder", "Document folder is invalid")
    path = PurePosixPath(candidate)
    if path.is_absolute():
        raise UploadRejectedError("invalid_document_folder", "Document folder must be relative")
    for part in path.parts:
        if part in {"", ".", ".."} or not part.isprintable():
            raise UploadRejectedError(
                "invalid_document_folder", "Document folder has an unsafe segment"
            )
    return path.as_posix()
