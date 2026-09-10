"""アップロードを storage へ渡す前に検証する pure なポリシー。"""

from __future__ import annotations

from dataclasses import dataclass

from skillmind.core.redaction import contains_sensitive_content

# 本文の機密走査は text 系 content-type にだけ適用する。
_TEXT_CONTENT_TYPES = frozenset(
    {"application/json", "application/yaml", "application/x-yaml", "image/svg+xml"}
)


class UploadRejectedError(ValueError):
    """アップロードが size/種別/機密/配額 policy に反することを表す。"""

    def __init__(self, code: str, message: str) -> None:
        """安定 code と、来源値を含まない短い説明を保持する。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """一ファイルと Project 単位の上限、許可 content-type を保持する。"""

    max_bytes: int
    project_quota_bytes: int
    allowed_content_types: frozenset[str]

    def validate(
        self,
        *,
        size: int,
        content_type: str,
        content: bytes,
        project_usage_bytes: int,
    ) -> str:
        """Size/種別/機密/配額を fail-closed に検証し、正規化 content-type を返す。"""

        normalized = normalize_content_type(content_type)
        if size <= 0:
            raise UploadRejectedError("empty_upload", "Uploaded file is empty")
        if size > self.max_bytes:
            raise UploadRejectedError("file_too_large", "Uploaded file exceeds the size limit")
        if project_usage_bytes + size > self.project_quota_bytes:
            raise UploadRejectedError("project_quota_exceeded", "Project storage quota is exceeded")
        if normalized not in self.allowed_content_types:
            raise UploadRejectedError("content_type_not_allowed", "Content type is not allowed")
        if _is_text(normalized) and contains_sensitive_content(_decode(content)):
            raise UploadRejectedError(
                "sensitive_content", "Upload contains credential-like content"
            )
        return normalized


def normalize_content_type(content_type: str) -> str:
    """`; charset=` などの parameter を落とし、小文字化した media type を返す。"""

    return content_type.split(";", 1)[0].strip().lower()


def _is_text(content_type: str) -> bool:
    """機密走査対象の text 系 content-type かを判定する。"""

    return content_type.startswith("text/") or content_type in _TEXT_CONTENT_TYPES


def _decode(content: bytes) -> str:
    """機密走査のため UTF-8 として寛容にデコードする。"""

    return content.decode("utf-8", errors="ignore")
