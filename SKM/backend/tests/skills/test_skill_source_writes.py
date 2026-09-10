"""共通 source 展開で入力 path の拒否と実環境障害を区別し、SQL/外部 PUT は使わない。"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import pytest

from skillmind.skills.domain import InlineSkillFile, SkillPreview, UploadSkillFile
from skillmind.skills.importer import SkillImportError
from skillmind.skills.service import (
    SkillService,
    _write_inline_skill_files,
    _write_upload_skill_files,
)
from tests.skills.skill_import_authorization_harness import INLINE_FILES, ImportSession

Entry = Literal["preview-inline", "preview-upload", "write-inline", "write-upload"]
ENTRIES: tuple[Entry, ...] = (
    "preview-inline",
    "preview-upload",
    "write-inline",
    "write-upload",
)
INPUT_ERRNOS = (errno.ENAMETOOLONG, errno.EEXIST, errno.ENOTDIR, errno.EISDIR)
ENVIRONMENT_ERRNOS = (errno.ENOSPC, errno.EACCES, errno.EIO, errno.EROFS)
PATH_ERROR = "Skill source contains a path that cannot be materialized"


def invoke(
    entry: Entry,
    service: SkillService,
    root: Path,
    files: tuple[InlineSkillFile, ...],
) -> SkillPreview | None:
    """同じ原入力を preview と直接 writer へ渡し、共有実装への各入口を検査する。"""

    uploads = tuple(
        UploadSkillFile(path=file.path, data=file.content.encode(), content_type="text/plain")
        for file in files
    )
    if entry == "preview-inline":
        return service.preview_inline(files)
    if entry == "preview-upload":
        return service.preview_upload(uploads)
    if entry == "write-inline":
        _write_inline_skill_files(files, root)
    else:
        _write_upload_skill_files(uploads, root)
    return None


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("stage", ["mkdir", "write_bytes"])
@pytest.mark.parametrize("error_number", INPUT_ERRNOS + ENVIRONMENT_ERRNOS)
def test_source_writes_classify_only_input_path_errnos_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: Entry,
    stage: str,
    error_number: int,
) -> None:
    """同一 OSError を一度だけ観測し、容量/権限/I/O 障害を入力 400 へ偽装しない。"""

    session = ImportSession()
    service = session.service()
    error = OSError(error_number, "Synthetic private disk detail", "/synthetic/private/source")
    operation = Mock(side_effect=error)
    with monkeypatch.context() as patch:
        patch.setattr(Path, stage, operation)
        if error_number in INPUT_ERRNOS:
            with pytest.raises(SkillImportError) as rejected:
                invoke(entry, service, tmp_path, INLINE_FILES)
        else:
            with pytest.raises(OSError) as failed:
                invoke(entry, service, tmp_path, INLINE_FILES)
    operation.assert_called_once()
    if error_number in INPUT_ERRNOS:
        assert rejected.value.code == "invalid_file_path"
        assert rejected.value.message == PATH_ERROR and rejected.value.path is None
        assert rejected.value.__cause__ is error
        assert "private" not in str(rejected.value)
    else:
        assert failed.value is error
    assert session.events == [] and session.timeline == []
    assert session.sources == [] and session.interpretations == []
    assert session.storage.attempts == [] and session.storage.deletions == []


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    "paths",
    [
        pytest.param(("界" * 86 + ".txt",), id="utf8-name-exceeds-byte-limit"),
        pytest.param(("x" * 260,), id="ascii-name-exceeds-byte-limit"),
        pytest.param(("a", "a/b.txt"), id="file-before-directory"),
        pytest.param(("a/b.txt", "a"), id="directory-before-file"),
    ],
)
def test_source_writes_reject_real_temporary_path_failures(
    tmp_path: Path,
    entry: Entry,
    paths: tuple[str, ...],
) -> None:
    """実 tmp の filename byte 制限と両順序の file/directory 衝突を再現する。"""

    session = ImportSession()
    service = session.service()
    files = (
        INLINE_FILES[0],
        *(InlineSkillFile(path=path, content="Synthetic private source body") for path in paths),
    )
    with pytest.raises(SkillImportError) as rejected:
        invoke(entry, service, tmp_path, files)
    assert rejected.value.code == "invalid_file_path"
    assert rejected.value.message == PATH_ERROR and rejected.value.path is None
    cause = rejected.value.__cause__
    assert isinstance(cause, OSError) and cause.errno in INPUT_ERRNOS
    assert str(tmp_path) not in str(rejected.value)
    assert session.events == [] and session.timeline == []
    assert session.storage.attempts == [] and session.storage.deletions == []


@pytest.mark.parametrize("entry", ENTRIES)
def test_source_writes_preserve_valid_unicode_paths_and_original_utf8_bytes(
    tmp_path: Path,
    entry: Entry,
) -> None:
    """入力拒否の共通化で、合法な相対 path、CRLF、UTF-8 bytes を変換しない。"""

    session = ImportSession()
    service = session.service()
    files = (
        INLINE_FILES[0],
        InlineSkillFile(path="references/資料.txt", content="合成資料\r\noriginal\r\n"),
    )
    result = invoke(entry, service, tmp_path, files)
    if entry.startswith("write-"):
        assert result is None
        for file in files:
            assert (tmp_path / file.path).read_bytes() == file.content.encode()
    else:
        assert isinstance(result, SkillPreview)
        assert result == service.preview_inline(files)
    assert session.events == [] and session.storage.attempts == []
