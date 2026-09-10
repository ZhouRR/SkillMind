"""Runtime に構成済みの保存先と、原文書の非公開参照だけを照合する。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.storage.blob import FileStorage, FileStorageError, StorageNamespace


def canonical_s3_endpoint(endpoint: str) -> str:
    """SDK が無視する URL 成分を拒否し、記録する宛先と client の解釈を一致させる。"""

    invalid = "Object storage endpoint is invalid"
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or not endpoint.isascii()
        or any(character.isspace() or not character.isprintable() for character in endpoint)
        or any(character in endpoint for character in "\\%?#")
    ):
        raise FileStorageError(invalid)
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise FileStorageError(invalid) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or port == 0
    ):
        raise FileStorageError(invalid)
    if ":" in host:
        if re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]{1,5})?", parsed.netloc) is None:
            raise FileStorageError(invalid)
        try:
            authority = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError as error:
            raise FileStorageError(invalid) from error
    else:
        if re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", parsed.netloc) is None:
            raise FileStorageError(invalid)
        if len(host) > 253 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        ):
            raise FileStorageError(invalid)
        authority = host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def make_s3_namespace(*, namespace_id: UUID, endpoint: str, bucket: str) -> StorageNamespace:
    """世代 UUID と非 Secret の実接続 descriptor を固定し、鍵の rotation と分ける。"""

    descriptor = {
        "version": 1,
        "provider": "s3",
        "endpoint": canonical_s3_endpoint(endpoint),
        "bucket": bucket,
        "key_prefix": "",
    }
    return StorageNamespace(
        namespace_id=namespace_id,
        descriptor_checksum=f"sha256:{sha256_hex(canonical_json(descriptor))}",
        durable=True,
    )


def require_storage_namespace(
    storage: FileStorage, expected: StorageNamespace | None,
) -> StorageNamespace:
    """不明/不一致なら I/O 前に拒否し、DB の値から client や credential を組み立てない。"""

    if not isinstance(expected, StorageNamespace) or storage.namespace != expected:
        raise FileStorageError("Document storage namespace is unavailable")
    return expected
