"""実 importer の原 file/index/hash を publish gate の fixture に渡す。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.skills.design_validation import SkillDesignSource
from projectmind.skills.importer import SkillPackageParser
from projectmind.skills.interpreter import load_inline_text_files


def directory_gate_source(
    manifest: Mapping[str, Any], source_root: Path, *, bind_source_hash: bool = False
) -> SkillDesignSource:
    """原 fixture を読む。合成 native 候補だけ明示的に実 source へ新規束縛する。

    保存済み版の補修 helper ではない。既定では Manifest の identity/hash を変更せず、
    model fixture も実 importer の snapshot と同じ検査を通過しなければならない。
    """

    package = SkillPackageParser().parse_directory(source_root.resolve())
    files = load_inline_text_files(source_root, package)
    candidate = deepcopy(dict(manifest))
    identity = candidate["identity"]
    assert isinstance(identity, dict)
    if bind_source_hash:
        identity["source_hash"] = package.content_hash
        candidate["capability_blueprint"]["identity"]["source_hash"] = package.content_hash
    return SkillDesignSource(
        skill_key=identity["skill_key"],
        manifest_checksum="sha256:" + sha256_hex(canonical_json(candidate)),
        manifest=candidate,
        source_hash=package.content_hash,
        source_file_index=[item.to_dict() for item in package.files],
        source_snapshot=[{"path": item.path, "content": item.content} for item in files],
        interpretation_id=UUID(identity["interpretation_id"]),
        interpreter_version=identity["interpreter_version"],
    )
