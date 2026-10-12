"""複数 Skill の選択・共有依存と保存済み source の不変性を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.direct_candidate import candidate_schema
from skillmind.skills.domain import InlineSkillFile, StoredSkillSource, UploadSkillFile
from skillmind.skills.importer import SkillImportError, SkillPackageParser
from skillmind.skills.interpreter import (
    InterpreterFixtureRunner,
    SkillStaticAnalyzer,
    build_interpreter_request,
    load_capability_catalog,
    load_interpreter_system_skill,
)
from skillmind.skills.source_loader import SkillSourceLoader
from skillmind.skills.source_storage import SkillSourceStorage
from skillmind.storage import InMemoryFileStorage

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


@pytest.fixture
def collection(tmp_path: Path) -> Path:
    """二つの独立 entry と共有 Schema、非対象資料を含む合成 source。"""
    files = {
        "skills/alpha/SKILL.md": b"---\r\nname: alpha\r\ndescription: Alpha\r\n---\r\n"
        b"# Alpha\r\n[result](../../schemas/result.schema.json)\r\n",
        "skills/alpha/scripts/check.py": b"# Do not execute during import.\r\n",
        "skills/alpha/assets/icon.bin": b"\x00\x01",
        "skills/beta/SKILL.md": b"---\nname: beta\ndescription: Beta\n---\n# Beta\n",
        "schemas/result.schema.json": b'{"$ref":"defs/base.schema.json"}',
        "schemas/defs/base.schema.json": b'{"type":"object"}',
        "knowledge/INDEX.md": b"# Unrelated\n[invalid](../../../not-uploaded)\n",
        "input.xlsx": b"\x00unrelated",
    }
    for name, raw in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return tmp_path.resolve()


def test_selected_entry_keeps_original_paths_and_only_its_dependencies(collection: Path) -> None:
    """共有 root 内の参照を保ち、別 Skill/知識庫/Excel を指示へ混ぜない。"""
    package = SkillPackageParser().parse_directory(collection, entry_path="skills/alpha/SKILL.md")
    assert package.name == "alpha"
    assert package.primary_instruction == "skills/alpha/SKILL.md"
    assert package.entry_path == "skills/alpha/SKILL.md"
    assert {file.path for file in package.files} == {
        "skills/alpha/SKILL.md",
        "skills/alpha/scripts/check.py",
        "skills/alpha/assets/icon.bin",
        "schemas/result.schema.json",
        "schemas/defs/base.schema.json",
    }
    assert package.resources.scripts == ("skills/alpha/scripts/check.py",)
    assert package.resources.assets == ("skills/alpha/assets/icon.bin",)
    assert "Beta" not in package.instruction_text


def test_multiple_entries_are_not_silently_parsed_as_a_generic_document(collection: Path) -> None:
    """入力が複数 Skill を含む場合、明示選択なしで別の Markdown を採用しない。"""
    with pytest.raises(SkillImportError) as failed:
        SkillPackageParser().parse_directory(collection)
    assert failed.value.code == "skill_selection_required"


def test_regular_json_data_does_not_invent_a_schema_dependency(collection: Path) -> None:
    """$ref を持つ例示 payload まで Schema とみなし、不必要な取込阻断を作らない。"""
    path = collection / "skills/alpha/assets/example.json"
    path.write_bytes(b'{"$ref":"opaque-record-id"}')
    package = SkillPackageParser().parse_directory(collection, entry_path="skills/alpha/SKILL.md")
    assert path.relative_to(collection).as_posix() in {file.path for file in package.files}


def test_hash_tracks_shared_dependencies_but_not_unrelated_files(collection: Path) -> None:
    """共有 Schema の変更は新しい source、非対象資料の変更は同じ source とする。"""
    parser = SkillPackageParser()
    original = parser.parse_directory(collection, entry_path="skills/alpha/SKILL.md")
    beta = parser.parse_directory(collection, entry_path="skills/beta/SKILL.md")
    assert beta.content_hash != original.content_hash
    (collection / "knowledge/INDEX.md").write_bytes(b"unrelated change")
    assert (
        parser.parse_directory(collection, entry_path="skills/alpha/SKILL.md").content_hash
        == original.content_hash
    )
    (collection / "schemas/defs/base.schema.json").write_bytes(b'{"type":"string"}')
    assert (
        parser.parse_directory(collection, entry_path="skills/alpha/SKILL.md").content_hash
        != original.content_hash
    )
    assert (
        parser.parse_directory(collection, entry_path="skills/beta/SKILL.md").content_hash
        == beta.content_hash
    )


@pytest.mark.parametrize("target", ["../../../outside.json", "/outside.json", "%2Foutside.json"])
def test_selected_references_still_cannot_escape_the_uploaded_root(
    collection: Path, target: str
) -> None:
    """共有参照を許しても、外部パスを source として読める権限は追加しない。"""
    (collection / "skills/alpha/SKILL.md").write_text(f"# Alpha\n[x]({target})\n")
    with pytest.raises(SkillImportError) as failed:
        SkillPackageParser().parse_directory(collection, entry_path="skills/alpha/SKILL.md")
    assert failed.value.code == "reference_escape"


@pytest.mark.parametrize(
    "entry", ["../SKILL.md", "/SKILL.md", "skills/missing/SKILL.md", "input.xlsx"]
)
def test_selection_must_be_an_actual_uploaded_skill_entry(collection: Path, entry: str) -> None:
    """候補外の file と親パスを選択しても filesystem の読取範囲は増やさない。"""
    with pytest.raises(SkillImportError) as failed:
        SkillPackageParser().parse_directory(collection, entry_path=entry)
    assert failed.value.code == "skill_entry_not_found"


async def test_selected_source_reloads_with_identical_shared_bytes(collection: Path) -> None:
    """永続 bundle は選択 source だけを含み、後日の共有編集で既存 source が変化しない。"""
    storage = SkillSourceStorage(InMemoryFileStorage(), "fixture-bucket")
    loader = SkillSourceLoader(CONTRACTS, storage)
    files = tuple(
        UploadSkillFile(
            path.relative_to(collection).as_posix(), path.read_bytes(), "application/octet-stream"
        )
        for path in collection.rglob("*")
        if path.is_file()
    )
    prepared = loader.prepare_upload(files, entry_path="skills/alpha/SKILL.md")
    command = prepared.save_command(organization_id=UUID(int=1), imported_by=UUID(int=2))
    assert {file.path for file in prepared.upload_files} == {
        file["path"] for file in command.source_file_index
    }

    async def authorize() -> None:
        """この合成 test は blob の同一性のみ扱い、認可は既存 API 回帰で検証する。"""

    uri = await storage.store(
        organization_id=command.organization_id,
        source_hash=command.source_hash,
        files=prepared.upload_files,
        authorize=authorize,
    )
    stored = StoredSkillSource(
        UUID(int=3),
        command.organization_id,
        command.name,
        command.source_type,
        command.source_hash,
        command.source_files,
        uri,
        command.source_file_index,
        entry_path=command.entry_path,
    )
    (collection / "schemas/result.schema.json").write_bytes(b"changed after upload")
    loaded = await loader.load_stored(stored)
    assert loaded.package.to_dict() == prepared.preview.normalized_package
    assert loaded.source_files == prepared.source_files
    assert next(
        file.content for file in loaded.source_files if file.path.endswith("SKILL.md")
    ).startswith("---\r\n")

    # Blob から再構築した原 byte と選択入口を、実 compiler/公開門禁まで通す。
    identity = load_interpreter_system_skill(
        CONTRACTS.parent / "skills/skillmind-skill-interpreter",
        generation_schema=candidate_schema(CONTRACTS),
    )
    request = build_interpreter_request(
        package=loaded.package, source_files=loaded.source_files,
        analysis=SkillStaticAnalyzer().analyze(loaded.package, loaded.source_files),
        catalog=load_capability_catalog(CONTRACTS / "examples/skill-capability-catalog.v1.json"),
        system_skill=identity,
    )
    assert request["source"]["entry_path"] == command.entry_path
    candidate = json.loads((CONTRACTS / "examples/skill-candidate.v3.json").read_text())
    runner = InterpreterFixtureRunner(CONTRACTS)
    compiled = runner.run(request, candidate, bind_identity=True, require_direct_candidate=True)
    assert compiled["runtime_manifest_draft"]["source_documents"] == (
        request["source"]["source_documents"]
    )
    for invalid_entry in (None, "skills/beta/SKILL.md", "schemas/result.schema.json"):
        broken = deepcopy(request)
        if invalid_entry is None:
            del broken["source"]["entry_path"]
        else:
            broken["source"]["entry_path"] = invalid_entry
        with pytest.raises(CapabilityBlueprintError) as failed:
            runner.run(broken, candidate, bind_identity=True, require_direct_candidate=True)
        assert failed.value.code == "candidate_publish_invalid"


async def test_legacy_generic_source_does_not_acquire_a_new_entry(collection: Path) -> None:
    """旧 snapshot の再読取では、現在の検出規則で入口と指示を切り替えない。"""
    safe_root = collection / "legacy"
    safe_root.mkdir()
    path = safe_root / "references/SKILL.md"
    path.parent.mkdir()
    path.write_bytes(b"---\nname: nested\n---\n# Original\n")
    legacy = SkillPackageParser().parse_directory(safe_root, discover_entries=False)
    files = (InlineSkillFile("references/SKILL.md", path.read_bytes().decode()),)
    loader = SkillSourceLoader(CONTRACTS, SkillSourceStorage(None, "fixture-bucket"))
    source = StoredSkillSource(
        UUID(int=4), UUID(int=1), legacy.name, legacy.source_type, legacy.content_hash, files
    )
    loaded = await loader.load_stored(source)
    assert loaded.package.entry_path is None
    assert loaded.package.to_dict() == legacy.to_dict()
    selected = SkillPackageParser().parse_directory(safe_root, entry_path="references/SKILL.md")
    assert selected.content_hash != legacy.content_hash


async def test_single_nested_upload_persists_its_detected_entry(collection: Path) -> None:
    """候補が一つの upload でも、自動検出した入口を復旧に必要な値として保存する。"""
    loader = SkillSourceLoader(CONTRACTS, SkillSourceStorage(None, "fixture-bucket"))
    prepared = loader.prepare_upload(
        (
            UploadSkillFile(
                "skills/beta/SKILL.md",
                (collection / "skills/beta/SKILL.md").read_bytes(),
                "text/markdown",
            ),
            UploadSkillFile("input.xlsx", b"\x00unrelated", "application/octet-stream"),
        )
    )
    command = prepared.save_command(organization_id=UUID(int=1), imported_by=UUID(int=2))
    assert command.entry_path == "skills/beta/SKILL.md"
    assert len(prepared.upload_files) == 1
    source = StoredSkillSource(
        UUID(int=5),
        command.organization_id,
        command.name,
        command.source_type,
        command.source_hash,
        command.source_files,
        source_file_index=command.source_file_index,
        entry_path=command.entry_path,
    )
    loaded = await loader.load_stored(source)
    assert loaded.package.entry_path == command.entry_path
    assert loaded.package.to_dict() == prepared.preview.normalized_package
