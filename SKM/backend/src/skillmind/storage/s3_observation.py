"""HEAD と GET に同じ metadata 検証を適用する S3 読取境界。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime

from skillmind.storage.blob import FileStorageError
from skillmind.storage.observation import BlobObservation


def observation_from_headers(headers: object) -> BlobObservation:
    """SDK の size=0 等の補完を使わず、実 HTTP header の必須値を照合する。"""

    if not isinstance(headers, Mapping) or any(not isinstance(key, str) for key in headers):
        raise FileStorageError("Blob observation is invalid")
    values = {key.lower(): value for key, value in headers.items()}
    modified = values.get("last-modified")
    size = values.get("content-length")
    etag = values.get("etag")
    content_type = values.get("content-type")
    version = values.get("x-amz-version-id")
    if (
        not isinstance(modified, str)
        or not isinstance(size, str)
        or not size.isascii()
        or not size.isdecimal()
        or len(size) > 20
        or not isinstance(etag, str)
        or len(etag) < 3
        or not etag.startswith('"')
        or not etag.endswith('"')
        or not isinstance(content_type, str)
        or (version is not None and not isinstance(version, str))
    ):
        raise FileStorageError("Blob observation is invalid")
    try:
        timestamp = parsedate_to_datetime(modified)
        if timestamp.tzinfo is None:
            raise ValueError("Timezone is required")
        timestamp = timestamp.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as error:
        raise FileStorageError("Blob observation is invalid") from error
    return BlobObservation(
        last_modified=timestamp,
        etag=etag[1:-1],
        version_id=version,
        size=int(size),
        content_type=content_type,
    )
