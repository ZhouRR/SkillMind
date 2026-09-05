"""Browser directory upload の source root 正規化を検証する。"""

from __future__ import annotations

from pathlib import Path

from projectmind.skills import SkillService, UploadSkillFile

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


def test_preview_upload_removes_browser_directory_root() -> None:
    """共通 directory 名を除去し、SKILL.md を entry point として認識する。"""

    service = SkillService(None, CONTRACTS)  # type: ignore[arg-type]
    preview = service.preview_upload(
        (
            UploadSkillFile(
                path="repository-review/SKILL.md",
                data=b"---\nname: repository-review\n---\n# Repository Review\n",
                content_type="text/markdown",
            ),
            UploadSkillFile(
                path="repository-review/references/rules.md",
                data=b"# Rules\n",
                content_type="text/markdown",
            ),
        )
    )

    normalized = preview.normalized_package
    assert normalized["source"]["detected_adapter"] == "directory-skill/v1"
    assert [item["path"] for item in normalized["source"]["files"]] == [
        "SKILL.md",
        "references/rules.md",
    ]
