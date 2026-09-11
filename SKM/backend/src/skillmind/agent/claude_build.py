"""固定 SDK に同梱された CLI の実体を照合し、system CLI への fallback を閉じる。"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import _cli_version, _version

CLAUDE_AGENT_SDK_VERSION = "0.2.110"
CLAUDE_CODE_CLI_VERSION = "2.1.191"
_MAX_CLI_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BundledClaudeBuild:
    """照合済み配布物の identity。プロセスの停止や計量の完全性は証明しない。"""

    cli_path: Path
    cli_checksum: str
    sdk_version: str
    cli_version: str


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
            raise RuntimeError("Bundled Claude CLI changed during verification")
        checksum = hashlib.sha256()
        count = 0
        while chunk := source.read(1024 * 1024):
            count += len(chunk)
            if count > size:
                raise RuntimeError("Bundled Claude CLI size does not match its distribution")
            checksum.update(chunk)
        if (
            count != size
            or _stamp(os.fstat(source.fileno())) != stamp
            or _stamp(os.lstat(path)) != stamp
        ):
            raise RuntimeError("Bundled Claude CLI changed during verification")
    actual = base64.urlsafe_b64encode(checksum.digest()).decode("ascii").rstrip("=")
    if actual != digest:
        raise RuntimeError("Bundled Claude CLI checksum does not match its distribution")
    return checksum.hexdigest()


def bundled_claude_build() -> BundledClaudeBuild:
    """実 import 元と固定 wheel の RECORD を照合する。CLI 自体を起動しない。

    配布物 metadata は信頼した依存導入の一部であり署名/遠隔証明ではない。配備中の
    package 書換えを許可しない前提で、起動ごとに file identity を再確認する。
    """
    try:
        distribution = metadata.distribution("claude-agent-sdk")
        if (
            distribution.version != CLAUDE_AGENT_SDK_VERSION
            or _version.__version__ != CLAUDE_AGENT_SDK_VERSION
            or _cli_version.__cli_version__ != CLAUDE_CODE_CLI_VERSION
        ):
            raise RuntimeError("Claude runtime distribution version is not supported")
        package = Path(str(claude_agent_sdk.__file__)).resolve(strict=True).parent
        installed = Path(str(distribution.locate_file("claude_agent_sdk"))).resolve(strict=True)
        if package != installed:
            raise RuntimeError("Claude SDK import does not match its installed distribution")
        name = "claude.exe" if os.name == "nt" else "claude"
        relative = f"claude_agent_sdk/_bundled/{name}"
        entries = [entry for entry in distribution.files or () if entry.as_posix() == relative]
        if len(entries) != 1:
            raise RuntimeError("Bundled Claude CLI is missing from the distribution record")
        entry = entries[0]
        if (
            entry.hash is None
            or entry.hash.mode != "sha256"
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", entry.hash.value) is None
            or type(entry.size) is not int
            or not 0 < entry.size <= _MAX_CLI_BYTES
        ):
            raise RuntimeError("Bundled Claude CLI has an invalid distribution record")
        path = installed / "_bundled" / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != entry.size
            or (os.name != "nt" and not info.st_mode & 0o111)
            or path.resolve(strict=True) != path
        ):
            raise RuntimeError("Bundled Claude CLI is not the expected executable file")
        checksum = _verify_binary(str(path), entry.size, entry.hash.value, _stamp(info))
        return BundledClaudeBuild(
            path, checksum, distribution.version, _cli_version.__cli_version__
        )
    except (OSError, metadata.PackageNotFoundError) as error:
        # host の install path や PATH 候補を model/API の失敗本文へ露出しない。
        raise RuntimeError("Pinned Claude runtime distribution is unavailable") from error


def require_bundled_cli(path: str | Path | None) -> BundledClaudeBuild:
    """実 options の path が照合済み bundle を指す場合だけ、その build を返す。"""
    build = bundled_claude_build()
    if path is None or str(path) != str(build.cli_path):
        raise ValueError("Claude options do not select the pinned bundled CLI")
    return build
