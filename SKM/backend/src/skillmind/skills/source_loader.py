"""Preview・保存・再解釈が同じ原文解析と text snapshot を共有する境界。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from uuid import UUID

from skillmind.skills.domain import (
    InlineSkillFile,
    SaveSkillPreviewCommand,
    SkillPreview,
    SkillSourceIntegrityError,
    StoredSkillSource,
    UploadSkillFile,
)
from skillmind.skills.importer import (
    HTTP_SKILL_IMPORT_LIMITS,
    DeterministicManifestDraftBuilder,
    NormalizedSkillPackage,
    SkillPackageParser,
)
from skillmind.skills.source_storage import (
    SkillSourceStorage,
    freeze_upload_files,
    write_skill_source_files,
)


@dataclass(frozen=True, slots=True)
class LoadedSkillSource:
    """同じ一時展開から得た parser index と元 UTF-8 text。"""

    package: NormalizedSkillPackage
    source_files: tuple[InlineSkillFile, ...]


@dataclass(frozen=True, slots=True)
class PreparedSkillImport:
    """最初の transaction 前に固定した preview と永続化対象の元 file。"""

    preview: SkillPreview
    source_files: tuple[InlineSkillFile, ...]
    upload_files: tuple[UploadSkillFile, ...] = ()

    def save_command(self, *, organization_id: UUID, imported_by: UUID) -> SaveSkillPreviewCommand:
        """同じ解析産物から保存 command を構築し、保存用の再解析を不要にする。"""

        package = self.preview.normalized_package
        manifest = self.preview.runtime_manifest_draft
        source = _mapping(package, "source")
        metadata = _mapping(package, "metadata")
        identity = _mapping(manifest, "identity")
        compatibility = _mapping(manifest, "compatibility")
        extensions = _mapping(manifest, "extensions")
        diagnostics = compatibility.get("diagnostics")
        if not isinstance(diagnostics, list) or not all(
            isinstance(item, dict) for item in diagnostics
        ):
            raise RuntimeError("RuntimeManifest diagnostics must be an object array")
        confidence = compatibility.get("confidence")
        if not isinstance(confidence, int | float):
            raise RuntimeError("RuntimeManifest confidence must be numeric")
        return SaveSkillPreviewCommand(
            organization_id=organization_id,
            imported_by=imported_by,
            name=_string(metadata, "name"),
            source_type=_string(source, "type"),
            source_hash=_string(source, "content_hash"),
            source_files=self.source_files,
            interpreter_version=_string(identity, "interpreter_version"),
            compatibility_level=_string(compatibility, "level"),
            confidence=float(confidence),
            diagnostics=tuple(cast(dict[str, Any], item) for item in diagnostics),
            checksum=_string(extensions, "normalized_package_hash"),
            preview=self.preview,
            source_file_index=_package_file_index_from_source(source),
        )


class SkillSourceLoader:
    """Source の準備だけを所有し、認可/DB transaction/model の lifecycle は扱わない。"""

    def __init__(self, contracts_dir: Path, storage: SkillSourceStorage) -> None:
        """同じ parser 制限と deterministic manifest builder を全入口で利用する。"""

        schema_path = contracts_dir / "runtime-manifest" / "v1alpha1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ValueError("RuntimeManifest Schema must be a JSON object")
        self._manifest_builder = DeterministicManifestDraftBuilder(schema)
        self._parser = SkillPackageParser(limits=HTTP_SKILL_IMPORT_LIMITS)
        self._storage = storage

    def prepare_inline(self, files: Sequence[InlineSkillFile]) -> PreparedSkillImport:
        """呼出側 alias から切離し、inline snapshot の元 path/text を保持する。"""

        frozen_files = deepcopy(tuple(files))
        _loaded, preview = self._prepare(frozen_files)
        return PreparedSkillImport(preview=preview, source_files=frozen_files)

    def prepare_upload(self, files: Sequence[UploadSkillFile]) -> PreparedSkillImport:
        """一度だけ root/bytes を固定し、同じ package を preview と保存の両方に渡す。"""

        frozen_files = freeze_upload_files(files)
        loaded, preview = self._prepare(frozen_files)
        return PreparedSkillImport(
            preview=preview, source_files=loaded.source_files, upload_files=frozen_files,
        )

    def _prepare(
        self, files: Sequence[InlineSkillFile | UploadSkillFile],
    ) -> tuple[LoadedSkillSource, SkillPreview]:
        """Inline/upload/保存 preview の展開・parser・manifest 手順を共有する。"""

        with TemporaryDirectory(prefix="skillmind-skill-") as temporary:
            root = Path(temporary).resolve()
            write_skill_source_files(files, root)
            loaded = self._load_directory(root)
        return loaded, SkillPreview(
            normalized_package=loaded.package.to_dict(),
            runtime_manifest_draft=self._manifest_builder.build(loaded.package),
        )

    async def load_stored(self, source: StoredSkillSource) -> LoadedSkillSource:
        """原 URI からの展開と per-file/package hash を検証し、一時 file は成功/失敗とも閉じる。"""

        with TemporaryDirectory(prefix="skillmind-interpret-") as temporary:
            root = Path(temporary).resolve()
            await self._storage.materialize(source, root)
            loaded = self._load_directory(root)
        if loaded.package.content_hash != source.source_hash:
            raise SkillSourceIntegrityError(
                "Stored SkillSource content hash drifted from its snapshot"
            )
        return loaded

    def _load_directory(self, root: Path) -> LoadedSkillSource:
        """Parser の text/binary 判定を唯一の正本として元 bytes を読み込む。"""

        package = self._parser.parse_directory(root)
        return LoadedSkillSource(
            package=package, source_files=load_inline_text_files(root, package),
        )


def load_inline_text_files(
    root: Path, package: NormalizedSkillPackage
) -> tuple[InlineSkillFile, ...]:
    """Parser が index 済みの text file だけを offline runner 用に再読込する。"""

    resolved_root = root.resolve(strict=True)
    files: list[InlineSkillFile] = []
    for indexed in package.files:
        if indexed.binary:
            continue
        path = (resolved_root / indexed.path).resolve(strict=True)
        if not path.is_relative_to(resolved_root) or path.is_symlink():
            raise ValueError("Indexed Skill file escapes the source root")
        # source index の SHA-256 と同じ UTF-8 bytes を analyzer へ渡し、CRLF を LF へ変換しない。
        files.append(InlineSkillFile(path=indexed.path, content=path.read_bytes().decode("utf-8")))
    return tuple(files)


def _package_file_index_from_source(source: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Parser の source.files を型検証し、binary を含む file index を返す。"""

    files = source.get("files")
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise RuntimeError("Normalized package source.files must be an object array")
    return tuple(dict(cast(dict[str, Any], item)) for item in files)


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Parser output の object field を型付きで取得する。"""

    nested = value.get(key)
    if not isinstance(nested, dict):
        raise RuntimeError(f"Parser output field must be an object: {key}")
    return cast(dict[str, Any], nested)


def _string(value: Mapping[str, Any], key: str) -> str:
    """Parser output の必須 string field を取得する。"""

    item = value.get(key)
    if not isinstance(item, str):
        raise RuntimeError(f"Parser output field must be a string: {key}")
    return item

