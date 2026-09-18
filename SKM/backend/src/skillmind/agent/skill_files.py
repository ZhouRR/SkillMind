"""凍結 Skill の text byte と只読 input の位置を結ぶ。取得・翻訳・実行は行わない。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from skillmind.agent.domain import RunWorkspace
from skillmind.agent.materialization_storage import MaterializationError
from skillmind.core.hashing import canonical_json
from skillmind.runs.domain import ClaimedRun
from skillmind.runs.input_snapshot import InputFileSeal, parse_input_files
from skillmind.skills.frozen_manifest import verified_run_manifest
from skillmind.skills.source_documents import validate_source_documents

SKILL_FILES_ROOT = ".skillmind/skill"
SKILL_FILE_CAPABILITIES = frozenset({
    "workspace.read/v1", "workspace.search/v1", "json.schema.validate/v1",
})


def source_file_contents(
    documents: Sequence[Mapping[str, str]],
) -> tuple[tuple[InputFileSeal, bytes], ...]:
    """原 UTF-8 byte を保持し、衝突を file I/O 前に検出する。拡張子で分岐しない。"""
    if not documents:
        return ()
    validated = validate_source_documents(list(documents))
    contents = tuple(
        (InputFileSeal(f"{SKILL_FILES_ROOT}/{item['path']}",
                       len(item["content"].encode("utf-8")), item["sha256"]),
         item["content"].encode("utf-8"))
        for item in sorted(validated, key=lambda item: item["path"])
    )
    parse_input_files([seal.to_json() for seal, _ in contents])
    return contents


def frozen_file_contents(
    claimed: ClaimedRun, documents: Sequence[Mapping[str, str]],
) -> tuple[tuple[InputFileSeal, bytes], ...]:
    """引数の原文を原 Run の Manifest と照合し、別版の物化を防ぐ。"""
    if not documents:
        return ()
    manifest = verified_run_manifest(claimed.skill_snapshots_json, claimed.task_snapshot_json)
    if list(documents) != manifest.get("source_documents"):
        raise MaterializationError("Skill files do not match the frozen Run manifest")
    return source_file_contents(documents)


def reused_skill_files(
    workspace: RunWorkspace, expected: Sequence[InputFileSeal],
) -> tuple[InputFileSeal, ...]:
    """READY の封印集合だけを採用する。未物化の旧集合は補完せず、案内もしない。"""
    actual = tuple(seal for seal in workspace.input_files or ()
                   if seal.path.startswith(".skillmind/"))
    if not actual:
        return ()
    if actual != tuple(expected):
        raise MaterializationError("Sealed Skill files do not match the frozen source")
    return actual


def skill_file_locations(
    documents: Sequence[Mapping[str, str]], files: Sequence[InputFileSeal],
) -> list[dict[str, str]]:
    """準備済み全 file の位置と hash だけを Brief へ投影し、本文を二重に載せない。"""
    if not files:
        return []
    expected = tuple(seal for seal, _ in source_file_contents(documents))
    if tuple(files) != expected:
        raise MaterializationError("Skill file locations do not match the frozen source")
    prefix = SKILL_FILES_ROOT + "/"
    return [
        {"source_path": seal.path.removeprefix(prefix), "path": f"input/{seal.path}",
         "sha256": seal.checksum}
        for seal in files
    ]


def append_skill_file_guidance(sections: list[str], brief: Mapping[str, Any]) -> None:
    """実在 file の利用を案内し、明示された派生物の生成や元の権限を変更しない。"""
    if not brief.get("skill_files"):
        return
    sections.append(
        "Frozen Skill files (already sealed on disk): " + canonical_json(brief["skill_files"])
        + "\nUse these exact paths with the allowed workspace or JSON validation Tools. "
        "To validate against a bundled schema, pass its path directly as schema_path; "
        "do not rewrite, shorten or regenerate it merely to supply the validator. "
        "If the Skill explicitly requires a derived file, create it separately using "
        "already allowed Tools and distinguish it from the frozen original. Other frozen "
        "text files are equally available. File availability grants no script execution, "
        "external reference resolution or additional Tool permissions."
    )
