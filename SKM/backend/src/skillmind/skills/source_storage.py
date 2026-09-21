"""Skill 原文 bundle の安全な一時展開と、元 bytes の object storage 往復を所有する。"""

from __future__ import annotations

import errno
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from skillmind.core.hashing import sha256_hex
from skillmind.skills.domain import (
    InlineSkillFile,
    SkillSourceIntegrityError,
    SkillStorageUnavailableError,
    StoredSkillSource,
    UploadSkillFile,
)
from skillmind.skills.importer import SkillImportError
from skillmind.storage import FileStorage, FileStorageError, StoredBlob, sanitize_object_key


class SkillSourceStorage:
    """DB を所有せず、原資格 callback と source の immutable index で I/O を制御する。"""

    def __init__(self, storage: FileStorage | None, bucket: str) -> None:
        """保存時と再構築時に使う同じ storage/bucket を保持する。"""

        self._storage = storage
        self._bucket = bucket

    async def store(
        self,
        *,
        organization_id: UUID,
        source_hash: str,
        files: Sequence[UploadSkillFile],
        authorize: Callable[[], Awaitable[None]],
    ) -> str:
        """全 key を事前検証し、逐 PUT の資格再確認と原 byte 回执照合後に URI を返す。"""

        if self._storage is None:
            raise SkillStorageUnavailableError("Object storage is not configured for uploads")
        hash_segment = source_hash.replace(":", "-")
        key_prefix = f"organizations/{organization_id}/skill-sources/{hash_segment}"
        await _store_upload_bundle(self._storage, key_prefix, files, authorize=authorize)
        return f"s3://{self._bucket}/{key_prefix}"

    async def materialize(self, source: StoredSkillSource, root: Path) -> None:
        """保存時の URI と index だけから元 bytes を再構築し、現行 file で補修しない。"""

        if source.storage_uri.startswith("database://") or not source.storage_uri:
            write_skill_source_files(source.source_files, root)
            return
        if not source.storage_uri.startswith("s3://"):
            raise SkillSourceIntegrityError("Stored SkillSource storage URI is invalid")
        if self._storage is None:
            raise SkillStorageUnavailableError(
                "Object storage is not configured for stored SkillSource reconstruction"
            )
        prefix = _storage_uri_prefix(source.storage_uri, self._bucket)
        if not source.source_file_index:
            raise SkillSourceIntegrityError("Stored SkillSource file manifest is missing")
        seen: set[str] = set()
        for item in source.source_file_index:
            path, expected_size, expected_hash = _validate_source_file_index(item, seen)
            try:
                data = await self._storage.get(f"{prefix}/{path}")
            except FileStorageError as error:
                # 本文や backend error を公開せず、元 source が使えないことだけを伝える。
                raise SkillSourceIntegrityError(
                    f"Stored SkillSource file is unavailable: {path}"
                ) from error
            actual_hash = f"sha256:{sha256_hex(data)}"
            if len(data) != expected_size or actual_hash != expected_hash:
                raise SkillSourceIntegrityError(f"Stored SkillSource file checksum drifted: {path}")
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise SkillSourceIntegrityError("Stored SkillSource file path escapes source root")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def freeze_upload_files(files: Sequence[UploadSkillFile]) -> tuple[UploadSkillFile, ...]:
    """最初の await より前に bytes を複写し、browser directory の共通 root だけを除去する。"""

    return _normalize_upload_skill_root(tuple(
        UploadSkillFile(path=file.path, data=bytes(file.data), content_type=file.content_type)
        for file in files
    ))


def write_skill_source_files(
    files: Sequence[InlineSkillFile | UploadSkillFile], root: Path,
) -> None:
    """Inline/upload 共通で相対 path と重複を検証し、改行を変えずに一時展開する。"""

    seen: set[str] = set()
    for file in files:
        relative = _safe_skill_file_path(file.path)
        if relative in seen:
            raise SkillImportError(
                "duplicate_file_path", "Skill source contains duplicate file paths", path=relative,
            )
        seen.add(relative)
        data = file.content.encode("utf-8") if isinstance(file, InlineSkillFile) else file.data
        _write_skill_source_file(root, relative, data)


def _write_skill_source_file(root: Path, relative: str, data: bytes) -> None:
    """原 byte の一時展開を共有し、入力由来の path 衝突だけを安定した拒否へ変換する。"""

    try:
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise SkillImportError(
                "invalid_file_path",
                "Skill source file path must stay inside the source root",
                path=relative,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as error:
        # UTF-8 の実 filename 上限や file/directory の衝突は文字数では判定できない。
        # disk full/permission/I/O 障害を入力拒否と偽らず、storage key 規則も複製しない。
        if error.errno not in {errno.ENAMETOOLONG, errno.EEXIST, errno.ENOTDIR, errno.EISDIR}:
            raise
        raise SkillImportError(
            "invalid_file_path", "Skill source contains a path that cannot be materialized"
        ) from error


def _normalize_upload_skill_root(
    files: tuple[UploadSkillFile, ...],
) -> tuple[UploadSkillFile, ...]:
    """Browser directory 選択が付与する共通 root 名だけを除去する。

    直下に SKILL.md が既にある入力は変更しない。全 file が同じ先頭 directory を共有し、
    その directory を除去した結果だけが SKILL.md を root に置く場合に限定することで、通常の
    nested resource directory を誤って source root と解釈しない。
    """

    if not files:
        return files
    safe_paths = tuple(_safe_skill_file_path(file.path) for file in files)
    if "SKILL.md" in safe_paths:
        return files
    parts = tuple(PurePosixPath(path).parts for path in safe_paths)
    common_root = parts[0][0]
    if any(len(item) < 2 or item[0] != common_root for item in parts):
        return files
    stripped = tuple(PurePosixPath(*item[1:]).as_posix() for item in parts)
    if "SKILL.md" not in stripped:
        return files
    return tuple(replace(file, path=path) for file, path in zip(files, stripped, strict=True))


async def _store_upload_bundle(
    storage: FileStorage,
    key_prefix: str,
    files: Sequence[UploadSkillFile],
    *,
    authorize: Callable[[], Awaitable[None]],
) -> None:
    """原資格の短 transaction の間で raw bundle を書き、応答を原 byte と照合する。"""

    targets: list[tuple[str, UploadSkillFile]] = []
    for file in files:
        relative = _safe_skill_file_path(file.path)
        key = f"{key_prefix}/{relative}"
        try:
            if sanitize_object_key(key) != key:
                raise FileStorageError("Object key must already be normalized")
        except FileStorageError as error:
            # 後半 file の不正 key で前半だけ PUT することを避け、全 target を先に検証する。
            raise SkillImportError(
                "invalid_file_path", "Skill source file path cannot be stored"
            ) from error
        targets.append((key, file))
    for key, file in targets:
        await authorize()
        content_type = file.content_type or "application/octet-stream"
        try:
            stored = await storage.put(key, file.data, content_type=content_type)
        except FileStorageError as error:
            # SDK 失敗でも byte が残り得る。原資格を再検証し、共有 key の補償削除はしない。
            await authorize()
            raise SkillStorageUnavailableError("Skill source storage is unavailable") from error
        await authorize()
        if (
            not isinstance(stored, StoredBlob)
            or stored.key != key
            or type(stored.size) is not int
            or stored.size != len(file.data)
            or stored.content_type != content_type
            or stored.sha256 != f"sha256:{sha256_hex(file.data)}"
        ):
            raise SkillStorageUnavailableError("Skill source storage is unavailable")


def _safe_skill_file_path(value: str) -> str:
    """POSIX 形式の相対 file path だけを API input として受理する。"""

    candidate = value.replace("\\", "/").strip()
    path = PurePosixPath(candidate)
    if (
        path.is_absolute()
        or PureWindowsPath(candidate).drive
        or not candidate
        or candidate.endswith("/")
        or not candidate.isprintable()
    ):
        raise SkillImportError(
            "invalid_file_path",
            "Skill source file path must be a relative file path",
            path=value,
        )
    # PurePath は dot/重複 separator を畳むため、正規化する前の segment で拒否する。
    if any(part in {"", ".", ".."} for part in candidate.split("/")):
        raise SkillImportError(
            "invalid_file_path",
            "Skill source file path must not contain dot segments",
            path=value,
        )
    return path.as_posix()


def _storage_uri_prefix(storage_uri: str, bucket: str) -> str:
    """保存時 bucket と一致する s3 URI から安全な object key prefix を取り出す。"""

    parsed = urlsplit(storage_uri)
    prefix = parsed.path.lstrip("/").rstrip("/")
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or not prefix
        or parsed.query
        or parsed.fragment
    ):
        raise SkillSourceIntegrityError("Stored SkillSource storage URI is invalid")
    try:
        return sanitize_object_key(prefix)
    except FileStorageError as error:
        raise SkillSourceIntegrityError("Stored SkillSource storage URI is invalid") from error


def _validate_source_file_index(item: Mapping[str, Any], seen: set[str]) -> tuple[str, int, str]:
    """Persisted file index の path、size、checksum を検証し、重複を拒否する。"""

    path = item.get("path")
    size = item.get("size")
    checksum = item.get("sha256")
    if not isinstance(path, str) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise SkillSourceIntegrityError("Stored SkillSource file manifest is invalid")
    if not isinstance(checksum, str) or not checksum.startswith("sha256:"):
        raise SkillSourceIntegrityError("Stored SkillSource file manifest is invalid")
    try:
        normalized = _safe_skill_file_path(path)
    except SkillImportError as error:
        raise SkillSourceIntegrityError("Stored SkillSource file manifest is invalid") from error
    if normalized in seen:
        raise SkillSourceIntegrityError("Stored SkillSource file manifest contains duplicates")
    seen.add(normalized)
    return normalized, size, checksum

