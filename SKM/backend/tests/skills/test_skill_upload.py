"""Multipart upload import (binary asset 可)の正規化・永続化・object storage 保存を検証する。"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest

from skillmind.db.models import SkillSource
from skillmind.skills import (
    InlineSkillFile,
    SkillService,
    SkillSourceIntegrityError,
    SkillStorageUnavailableError,
    StoredSkillSource,
    UploadSkillFile,
    load_capability_catalog,
    load_interpreter_system_skill,
)
from skillmind.storage import InMemoryFileStorage
from tests.skills.skill_import_authorization_harness import ImportSession

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
CATALOG = CONTRACTS / "examples" / "skill-capability-catalog.v1.json"
SYSTEM_SKILL = ROOT / "skills" / "skillmind-skill-interpreter"

# NUL を含むため parser は binary asset として扱う (テキスト snapshot には載らない)。
_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_SKILL_MD = "---\nname: Upload Skill\nallowed-tools: [Read]\n---\n# Upload Skill\n"


class _Transaction:
    """Test session の async transaction context。"""

    async def __aenter__(self) -> Self:
        """Transaction context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず transaction context を終了する。"""


class _EmptyResult:
    """Repository lookup に未登録を返す scalar result。"""

    def one_or_none(self) -> None:
        """既存 model がないことを返す。"""

        return None


class _Session:
    """SkillRepository が利用する最小 AsyncSession seam。"""

    def __init__(self) -> None:
        """追加された model を検査できるよう保持する。"""

        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        """Session context を開始する。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """Exception を抑制せず Session context を終了する。"""

    def begin(self) -> _Transaction:
        """Service が所有する transaction context を返す。"""

        return _Transaction()

    async def scalars(self, statement: object) -> _EmptyResult:
        """Source/interpretation lookup を未登録として扱う。"""

        del statement
        return _EmptyResult()

    def add(self, model: object) -> None:
        """Repository が追加した model を記録する。"""

        self.added.append(model)


class _SessionFactory:
    """同じ test session を返す session factory。"""

    def __init__(self, session: _Session) -> None:
        """Use case 後に model を確認する session を保持する。"""

        self._session = session

    def __call__(self) -> _Session:
        """Async context 対応 session を返す。"""

        return self._session


def _upload_files() -> tuple[UploadSkillFile, ...]:
    """SKILL.md (テキスト)と assets 配下の binary asset を持つ upload を作る。"""

    return (
        UploadSkillFile(
            path="SKILL.md", data=_SKILL_MD.encode("utf-8"), content_type="text/markdown"
        ),
        UploadSkillFile(path="assets/logo.png", data=_PNG_BYTES, content_type="image/png"),
    )


async def test_save_upload_stores_binary_bundle_and_text_only_snapshot() -> None:
    """Binary asset は object storage の bundle に入り、DB snapshot はテキストだけを持つ。"""

    session = ImportSession()
    storage = InMemoryFileStorage()
    service = session.service(storage)

    stored = await service.save_upload(
        access=session.access,
        files=_upload_files(),
    )

    source = next(model for model in session.added if isinstance(model, SkillSource))
    # storage_uri は object storage を指す s3:// になり、database:// 既定を使わない。
    assert source.storage_uri.startswith("s3://skillmind/organizations/")
    # DB snapshot はテキスト file だけ (binary は載せない)。
    assert [item["path"] for item in source.source_snapshot_json] == ["SKILL.md"]
    # 正規化 package は binary asset を resources.assets と file index に記録する。
    normalized = stored.preview.normalized_package
    assert "assets/logo.png" in normalized["resources"]["assets"]
    png_entry = next(
        item for item in normalized["source"]["files"] if item["path"] == "assets/logo.png"
    )
    assert png_entry["binary"] is True
    # bundle は object storage へ round-trip 可能 (fake backend)。
    prefix = source.storage_uri.removeprefix("s3://skillmind/")
    assert await storage.get(f"{prefix}/assets/logo.png") == _PNG_BYTES
    assert await storage.get(f"{prefix}/SKILL.md") == _SKILL_MD.encode("utf-8")
    assert {item["path"] for item in source.source_file_index_json} == {
        "SKILL.md",
        "assets/logo.png",
    }


async def test_upload_interpret_reconstructs_original_bytes_and_checks_each_file() -> None:
    """Upload source が binary/CRLF を含んでも bundle 再構築後に hash 検証を通過する。"""

    session = ImportSession()
    storage = InMemoryFileStorage()
    service = session.service(storage)
    crlf_skill = _SKILL_MD.replace("\n", "\r\n")
    stored = await service.save_upload(
        access=session.access,
        files=(
            UploadSkillFile(
                path="SKILL.md",
                data=crlf_skill.encode("utf-8"),
                content_type="text/markdown",
            ),
            UploadSkillFile(path="assets/logo.png", data=_PNG_BYTES, content_type="image/png"),
        ),
    )
    source = next(model for model in session.added if isinstance(model, SkillSource))
    stored_source = StoredSkillSource(
        skill_source_id=source.id,
        organization_id=source.organization_id,
        name=source.name,
        source_type=source.source_type,
        source_hash=source.content_hash,
        source_files=(InlineSkillFile(path="SKILL.md", content=crlf_skill),),
        storage_uri=source.storage_uri,
        source_file_index=tuple(source.source_file_index_json),
    )
    package, _, _ = await service._prepare_request(
        stored_source,
        load_capability_catalog(CATALOG),
        load_interpreter_system_skill(SYSTEM_SKILL),
    )

    assert package.content_hash == stored.source_hash
    assert package.files[-1].path == "assets/logo.png"

    prefix = source.storage_uri.removeprefix("s3://skillmind/")
    await storage.put(
        f"{prefix}/assets/logo.png",
        b"tampered",
        content_type="image/png",
    )
    with pytest.raises(SkillSourceIntegrityError):
        await service._prepare_request(
            stored_source,
            load_capability_catalog(CATALOG),
            load_interpreter_system_skill(SYSTEM_SKILL),
        )


async def test_save_upload_requires_configured_storage() -> None:
    """Object storage 未配線なら upload は fail-closed で 503 相当の error を送出する。"""

    session = ImportSession()
    service = session.service(storage_configured=False)

    with pytest.raises(SkillStorageUnavailableError):
        await service.save_upload(
            access=session.access,
            files=_upload_files(),
        )


def test_preview_upload_matches_inline_for_text_only_source() -> None:
    """テキストのみの source では upload の正規化結果が inline 経路と一致する。"""

    service = SkillService(_SessionFactory(_Session()), CONTRACTS)  # type: ignore[arg-type]

    inline = service.preview_inline((InlineSkillFile(path="SKILL.md", content=_SKILL_MD),))
    upload = service.preview_upload(
        (
            UploadSkillFile(
                path="SKILL.md", data=_SKILL_MD.encode("utf-8"), content_type="text/markdown"
            ),
        )
    )

    assert upload.normalized_package == inline.normalized_package
