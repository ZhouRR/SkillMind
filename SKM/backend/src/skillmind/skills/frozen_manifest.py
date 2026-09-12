"""Run の唯一の Skill snapshot と Task の原 Manifest identity を共通検証する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from skillmind.core.hashing import canonical_json, sha256_hex


def verified_run_manifest(
    snapshots: Sequence[Mapping[str, Any]], task: Mapping[str, Any]
) -> Mapping[str, Any]:
    """実行と前置条件の照合で、別版・改変 Manifest を同じ規則で拒否する。"""

    if len(snapshots) != 1:
        raise ValueError("Run must bind exactly one SkillVersion snapshot")
    snapshot = snapshots[0]
    manifest, checksum = snapshot.get("manifest"), snapshot.get("manifest_checksum")
    if not isinstance(manifest, dict) or not isinstance(checksum, str):
        raise ValueError("Run SkillVersion snapshot is incomplete")
    if checksum != f"sha256:{sha256_hex(canonical_json(manifest))}" or (
        task.get("manifest_checksum") != checksum
    ):
        raise ValueError("Run SkillVersion Manifest checksum does not match frozen content")
    if task.get("skill_version_id") != snapshot.get("skill_version_id"):
        raise ValueError("Task and SkillVersion snapshot identity do not match")
    return manifest
