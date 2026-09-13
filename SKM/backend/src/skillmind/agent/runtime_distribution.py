"""SDK 共通の同梱 executable を、wheel RECORD と file identity で照合する。"""

from __future__ import annotations

import base64
import hashlib
import os


def _stamp(value: os.stat_result) -> tuple[int, ...]:
    """同じ大きさの書換えや権限変更も、以前の hash 確認から区別する。"""
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mode,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verify_binary(path: str, size: int, digest: str, stamp: tuple[int, ...]) -> str:
    """毎回 RECORD と実体を比較する。stat の時刻だけを改変検出の根拠にしない。"""
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as source:
        if _stamp(os.fstat(source.fileno())) != stamp:
            raise RuntimeError("Bundled CLI changed during verification")
        checksum = hashlib.sha256()
        count = 0
        while chunk := source.read(1024 * 1024):
            count += len(chunk)
            if count > size:
                raise RuntimeError("Bundled CLI size does not match its distribution")
            checksum.update(chunk)
        if (
            count != size
            or _stamp(os.fstat(source.fileno())) != stamp
            or _stamp(os.lstat(path)) != stamp
        ):
            raise RuntimeError("Bundled CLI changed during verification")
    actual = base64.urlsafe_b64encode(checksum.digest()).decode("ascii").rstrip("=")
    if actual != digest:
        raise RuntimeError("Bundled CLI checksum does not match its distribution")
    return checksum.hexdigest()


