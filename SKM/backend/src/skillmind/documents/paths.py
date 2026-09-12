"""受信と保存済み原要求で同じ文書 path/key 規則を用いる。"""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID

from skillmind.storage import FileStorageError, UploadRejectedError, sanitize_object_key


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


def document_effect_storage_key(project_id: UUID, folder: str, name: str) -> str:
    """成果の相対 path を Project 専用 prefix に固定し、既存 UUID upload と分離する。"""

    safe_folder, safe_name = validate_document_path(project_id=project_id, folder=folder, name=name)
    if (folder, name) != (safe_folder, safe_name):
        raise UploadRejectedError(
            "invalid_document_folder", "Document effect path must be canonical"
        )
    relative = "/".join(part for part in (folder, name) if part)
    return sanitize_object_key(f"{document_effect_prefix(project_id)}{relative}")


def document_effect_prefix(project_id: UUID) -> str:
    """成果 key と Run binding が同じ Project 専用 prefix を共有する。"""

    if not isinstance(project_id, UUID) or project_id.int == 0:
        raise ValueError("Document effect project is invalid")
    return f"projects/{project_id}/documents/effects/"


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
