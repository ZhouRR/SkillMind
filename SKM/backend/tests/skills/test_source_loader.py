"""源包の入口間で元 bytes を一致させ、失敗/取消後の一時展開を残さない。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest

from skillmind.core.hashing import sha256_hex
from skillmind.skills import source_loader
from skillmind.skills.domain import (
    InlineSkillFile,
    SkillSourceIntegrityError,
    StoredSkillSource,
    UploadSkillFile,
)
from skillmind.skills.source_loader import SkillSourceLoader
from skillmind.skills.source_storage import SkillSourceStorage
from skillmind.storage import InMemoryFileStorage

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SOURCE = (
    InlineSkillFile(
        "SKILL.md",
        "---\r\nname: sample\r\ndescription: sample\r\n---\r\n"
        "# 合成 source\r\n[本文](references/spec.md)\r\n",
    ),
    InlineSkillFile("references/spec.md", "原 byte の本文\r\n"),
)


async def test_inline_and_directory_upload_keep_one_identity_through_reconstruction() -> None:
    """Preview/永続 command/再解釈で root・CRLF・UTF-8 の hash と索引を変えない。"""

    blobs = InMemoryFileStorage()
    storage = SkillSourceStorage(blobs, "synthetic-bucket")
    loader = SkillSourceLoader(CONTRACTS, storage)
    inline = loader.prepare_inline(SOURCE)
    uploaded = loader.prepare_upload(tuple(
        UploadSkillFile(f"browser-root/{file.path}", file.content.encode(), "text/markdown")
        for file in SOURCE
    ))
    inline_command = inline.save_command(organization_id=UUID(int=1), imported_by=UUID(int=2))
    upload_command = uploaded.save_command(organization_id=UUID(int=1), imported_by=UUID(int=2))
    assert inline_command == upload_command
    checks: list[int] = []

    async def authorize() -> None:
        """I/O 前後を数える。実 actor/transaction は既存 service 回帰で検証する。"""

        checks.append(len(checks))

    uri = await storage.store(
        organization_id=upload_command.organization_id,
        source_hash=upload_command.source_hash,
        files=uploaded.upload_files,
        authorize=authorize,
    )
    assert len(checks) == len(SOURCE) * 2
    original = StoredSkillSource(
        skill_source_id=UUID(int=3),
        organization_id=inline_command.organization_id,
        name=inline_command.name,
        source_type=inline_command.source_type,
        source_hash=inline_command.source_hash,
        source_files=inline_command.source_files,
        source_file_index=inline_command.source_file_index,
    )
    for stored in (original, replace(original, storage_uri=uri)):
        loaded = await loader.load_stored(stored)
        assert loaded.package.to_dict() == inline.preview.normalized_package
        assert loaded.source_files == SOURCE
        assert tuple(
            (item.path, item.sha256) for item in loaded.package.files
        ) == tuple((file.path, f"sha256:{sha256_hex(file.content.encode())}") for file in SOURCE)


@pytest.mark.parametrize("failure", ["checksum", "cancelled"])
async def test_failed_stored_source_load_cleans_partial_tree_and_preserves_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """最初の file 展開後に失敗しても tree を閉じ、取消を通常の storage error へ変換しない。"""

    monkeypatch.setattr(
        source_loader, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path),
    )
    blobs = InMemoryFileStorage()
    storage = SkillSourceStorage(blobs, "synthetic-bucket")
    loader = SkillSourceLoader(CONTRACTS, storage)
    prepared = loader.prepare_upload(tuple(
        UploadSkillFile(file.path, file.content.encode(), "text/markdown") for file in SOURCE
    ))
    command = prepared.save_command(organization_id=UUID(int=1), imported_by=UUID(int=2))

    async def authorize() -> None:
        """この合成ケースでは純粋に展開 resource lifetime だけを対象にする。"""

    uri = await storage.store(
        organization_id=command.organization_id, source_hash=command.source_hash,
        files=prepared.upload_files, authorize=authorize,
    )
    original_get = blobs.get
    cancellation = asyncio.CancelledError("synthetic cancellation")
    observed: list[str] = []

    async def fail_on_reference(key: str, *, max_bytes: int | None = None) -> bytes:
        """二番目の file だけに異常を注入し、一番目が既に展開されたことを確認する。"""

        observed.append(key)
        if key.endswith("references/spec.md"):
            assert len(list(tmp_path.glob("*/SKILL.md"))) == 1
            if failure == "cancelled":
                raise cancellation
            return b"tampered"
        return await original_get(key, max_bytes=max_bytes)

    monkeypatch.setattr(blobs, "get", fail_on_reference)
    stored = StoredSkillSource(
        skill_source_id=UUID(int=3), organization_id=command.organization_id,
        name=command.name, source_type=command.source_type, source_hash=command.source_hash,
        source_files=command.source_files, storage_uri=uri,
        source_file_index=command.source_file_index,
    )
    error_type = asyncio.CancelledError if failure == "cancelled" else SkillSourceIntegrityError
    with pytest.raises(error_type) as failed:
        await loader.load_stored(stored)
    if failure == "cancelled":
        assert failed.value is cancellation
    assert len(observed) == 2
    assert list(tmp_path.iterdir()) == []
