"""Run workspace の file I/O を固定 root と directory descriptor の境界に閉じ込める。"""

from __future__ import annotations

import os
import stat
from collections.abc import Generator, Iterator, Sequence, Set
from contextlib import closing, contextmanager, suppress
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from skillmind.core.hashing import sha256_hex
from skillmind.runs.input_snapshot import InputFileSeal


class MaterializationError(RuntimeError):
    """不完全または信頼できない資源副本を Agent に渡さないための失敗信号。"""


class FileReadLimitError(MaterializationError):
    """取得した byte 数と呼出し側が許可した読取上限を区別する。"""


class UnsafeWorkspaceFileError(MaterializationError):
    """既存 link・特殊 file を普通の作業 file として上書きしない。"""


def relative_parts(path: str) -> tuple[str, ...]:
    """正規化で不正要素を消す前に、POSIX 相対 path を検証する。"""

    parts = tuple(path.split("/"))
    if "\\" in path or not path.isprintable() or any(part in {"", ".", ".."} for part in parts):
        raise MaterializationError("Materialized path is invalid")
    return parts


@contextmanager
def _directory_descriptor(
    root: Path, parts: Sequence[str], *, create: bool = False
) -> Iterator[int]:
    """途中の directory も no-follow で開き、rename 後も同じ境界を保持する。"""

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _parent_descriptor(root: Path, path: str, *, create: bool) -> Iterator[tuple[int, str]]:
    """検証した相対 path の親だけを descriptor として貸し出す。"""

    parts = relative_parts(path)
    with _directory_descriptor(root, parts[:-1], create=create) as descriptor:
        yield descriptor, parts[-1]


def input_generation_relative(snapshot_id: UUID) -> str:
    """DB が発行した世代だけを、固定の非公開 namespace へ対応させる。"""

    if not isinstance(snapshot_id, UUID):
        raise MaterializationError("Input generation identity is invalid")
    return f".skillmind-inputs/{snapshot_id}"


def create_generation(root: Path, snapshot_id: UUID) -> None:
    """namespace ごと独占作成し、失われた回执の候補へ二份目を追加しない。"""

    namespace, generation = relative_parts(input_generation_relative(snapshot_id))
    try:
        with _directory_descriptor(root, ()) as run:
            # 同じ UUID だけの排他では、DB を失った後の別 UUID を許してしまう。
            # 空 directory も中断の証拠として残し、再試行で存在を無視しない。
            os.mkdir(namespace, mode=0o700, dir_fd=run)
            os.fsync(run)
            parent = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run)
            try:
                with os.scandir(parent) as entries:
                    if next(entries, None) is not None:
                        raise MaterializationError("Input namespace changed during creation")
                os.mkdir(generation, mode=0o700, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
    except OSError as error:
        raise MaterializationError("Input generation could not be created safely") from error


def write_new_file(root: Path, path: str, data: bytes) -> None:
    """新規 file だけを書き、既存 file・link・途中までの副本を上書きしない。"""

    try:
        with _parent_descriptor(root, path, create=True) as (parent, name):
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fchmod(output.fileno(), 0o400)
                os.fsync(output.fileno())
            os.fsync(parent)
    except OSError as error:
        raise MaterializationError("Materialized file could not be created safely") from error


def read_file(root: Path, path: str, *, max_bytes: int) -> bytes:
    """通常 file のみを上限付きで読み、link・FIFO・device を取得前に拒否する。"""

    try:
        with _parent_descriptor(root, path, create=False) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise MaterializationError(
                        "Materialized input must be a regular single-link file"
                    )
                if info.st_size > max_bytes:
                    raise FileReadLimitError("Workspace file exceeds its read limit")
                data = source.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise FileReadLimitError("Workspace file exceeds its read limit")
                return data
    except OSError as error:
        raise MaterializationError("Materialized file could not be read safely") from error


def read_sealed_file(root: Path, *, path: str, seal: InputFileSeal, max_bytes: int) -> bytes:
    """回执に一致する実 byte を返し、検証後に別 path を開き直す競争を作らない。"""

    if seal.size > max_bytes:
        raise FileReadLimitError("Sealed file exceeds its read limit")
    data = read_file(root, path, max_bytes=seal.size)
    if len(data) != seal.size or f"sha256:{sha256_hex(data)}" != seal.checksum:
        raise MaterializationError("Input bytes do not match their trusted receipt")
    return data


def write_workspace_file(root: Path, path: str, data: bytes) -> bool:
    """同じ親 descriptor 内の新 inode へ書き、他の file を truncate せず原子的に置換する。"""

    try:
        with _parent_descriptor(root, path, create=True) as (parent, name):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                created = True
            else:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise UnsafeWorkspaceFileError("Workspace target is not a safe regular file")
                created = False
            temporary = f".skillmind-write-{uuid4().hex}"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                # 検査後に target が置換されても、その inode を辿って書かない。
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)
            return created
    except OSError as error:
        raise MaterializationError("Workspace file could not be written safely") from error


def verify_input_files(root: Path, *, relative: str, files: Sequence[InputFileSeal]) -> None:
    """全世代の tree と実 byte を、ファイルシステム外から得た回执で検証する。"""

    verify_tree(root, relative=relative, expected_files={item.path for item in files})
    for item in files:
        read_sealed_file(root, path=f"{relative}/{item.path}", seal=item, max_bytes=item.size)


def seal_generation(root: Path, *, relative: str, files: Sequence[InputFileSeal]) -> None:
    """完成 file を検証し、全 directory を只読で fsync してから公開可能にする。"""

    verify_input_files(root, relative=relative, files=files)
    parents = {
        parent.as_posix()
        for item in files
        for parent in PurePosixPath(item.path).parents
        if parent.as_posix() != "."
    }
    try:
        for directory in sorted(parents, key=lambda value: value.count("/"), reverse=True):
            with _directory_descriptor(root, relative_parts(f"{relative}/{directory}")) as fd:
                os.fchmod(fd, 0o500)
                os.fsync(fd)
        with _directory_descriptor(root, relative_parts(relative)) as fd:
            os.fchmod(fd, 0o500)
            os.fsync(fd)
    except OSError as error:
        raise MaterializationError("Input generation could not be sealed safely") from error


def verify_tree(root: Path, *, expected_files: Set[str], relative: str | None = None) -> None:
    """manifest 外の file・空 directory・link を副本へ混入させない。"""

    expected_directories = {
        parent.as_posix()
        for path in expected_files
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    found: set[str] = set()
    directories: set[str] = set()
    try:
        with closing(_tree_entries(root, relative)) as entries:
            for path, info in entries:
                if stat.S_ISDIR(info.st_mode):
                    if path not in expected_directories or path in directories:
                        raise MaterializationError("Materialized tree has an unexpected directory")
                    directories.add(path)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise MaterializationError("Materialized tree contains an unsafe file")
                elif path not in expected_files or path in found:
                    raise MaterializationError("Materialized tree has an unexpected file")
                else:
                    found.add(path)
    except OSError as error:
        raise MaterializationError("Materialized tree could not be inspected") from error
    if found != expected_files:
        raise MaterializationError("Materialized tree does not match its manifest")


def list_workspace_files(
    root: Path, relative: str, *, max_files: int, max_entries: int
) -> tuple[tuple[str, ...], bool]:
    """可変 workspace を有界列挙し、link を追わず探索の打切りを呼出し側へ伝える。"""

    found: list[str] = []
    try:
        with closing(_tree_entries(root, relative)) as entries:
            for index, (path, info) in enumerate(entries):
                if index >= max_entries:
                    return tuple(sorted(found)), True
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if len(found) >= max_files:
                        return tuple(sorted(found)), True
                    found.append(path)
    except OSError as error:
        raise MaterializationError("Workspace tree could not be inspected safely") from error
    return tuple(sorted(found)), False


def _tree_entries(
    root: Path, relative: str | None
) -> Generator[tuple[str, os.stat_result], None, None]:
    """一括 listdir や再帰 symlink 追従を避け、固定 descriptor 内で各 entry を確認する。"""

    base = relative_parts(relative) if relative is not None else ()
    pending: list[tuple[str, ...]] = [()]
    while pending:
        parent = pending.pop()
        with (
            _directory_descriptor(root, (*base, *parent)) as descriptor,
            os.scandir(descriptor) as entries,
        ):
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                parts = (*parent, entry.name)
                yield "/".join(parts), info
                if stat.S_ISDIR(info.st_mode):
                    pending.append(parts)
