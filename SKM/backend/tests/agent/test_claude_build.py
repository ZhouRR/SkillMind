"""同梱 CLI の出所/実体を、実行せず小さい file fixture で検証する。"""

from __future__ import annotations

import base64
import hashlib
import os
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillmind.agent import claude_build as build


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """固定 RECORD と import 元を同じ仮 package に向け、SDK 本体を変更しない。"""
    package = tmp_path / "claude_agent_sdk"
    package.mkdir()
    module = package / "__init__.py"
    module.write_text("# synthetic fixture\n")
    root = package / "_bundled"
    root.mkdir()
    binary = root / ("claude.exe" if os.name == "nt" else "claude")
    payload = b"Synthetic CLI bytes; never executed."
    binary.write_bytes(payload)
    binary.chmod(0o755)
    entry = metadata.PackagePath(binary.relative_to(tmp_path).as_posix())
    entry.hash = metadata.FileHash(
        "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
    )
    entry.size = len(payload)
    distribution = SimpleNamespace(
        version=build.CLAUDE_AGENT_SDK_VERSION,
        files=[entry],
        locate_file=lambda relative: tmp_path / relative,
    )
    monkeypatch.setattr(build.metadata, "distribution", lambda _: distribution)
    monkeypatch.setattr(build.claude_agent_sdk, "__file__", str(module))
    yield binary, distribution, entry


def test_bundle_identity_matches_record_without_starting_cli(bundle):
    """OS では実行できない fixture でも、検査は file と RECORD だけに閉じる。"""
    binary, _, _ = bundle
    result = build.bundled_claude_build()
    assert result.cli_path == binary
    assert result.cli_checksum == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert build.require_bundled_cli(binary) == result


def test_binary_rewrite_is_checked_even_when_file_times_are_preserved(bundle):
    """同じ size/mtime を保った書換えを、以前の検査結果で通さない。"""
    binary, _, _ = bundle
    build.bundled_claude_build()
    before = binary.stat()
    binary.write_bytes(b"X" * before.st_size)
    os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(RuntimeError, match="checksum"):
        build.bundled_claude_build()


@pytest.mark.parametrize(
    "failure",
    ["missing", "symlink", "record-missing", "hash-mode", "size", "version", "import-root"],
)
def test_invalid_bundle_never_selects_system_cli(bundle, monkeypatch, failure):
    """不正な配布物を見つけたら、system CLI 候補の検索へ進まず閉じる。"""
    binary, distribution, entry = bundle
    if failure == "missing":
        binary.unlink()
    elif failure == "symlink":
        replacement = binary.with_name("other")
        binary.rename(replacement)
        binary.symlink_to(replacement)
    elif failure == "record-missing":
        distribution.files = []
    elif failure == "hash-mode":
        entry.hash = metadata.FileHash("md5=" + "a" * 43)
    elif failure == "size":
        entry.size += 1
    elif failure == "version":
        distribution.version = "0.0.0"
    else:
        monkeypatch.setattr(build.claude_agent_sdk, "__file__", str(binary))
    with pytest.raises(RuntimeError):
        build.bundled_claude_build()


@pytest.mark.parametrize("path", [None, "/unconfigured/claude", "claude"])
def test_client_options_require_explicit_exact_bundle(bundle, path):
    """PATH 探索と別 CLI の明示指定を、記録の version と混同させない。"""
    with pytest.raises(ValueError, match="do not select"):
        build.require_bundled_cli(path)
