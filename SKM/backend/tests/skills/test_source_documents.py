"""要約で失われた業務制約を、原 source から実行 prompt まで保全する回帰。"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from skillmind.agent.domain import RunLimits
from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.skills.design_validation import SkillDesignInvalidError, validate_skill_design
from skillmind.skills.importer import SkillPackageParser
from skillmind.skills.interpreter import (
    InterpreterFixtureRunner,
    SkillStaticAnalyzer,
    build_interpreter_generation_schema,
    build_interpreter_request,
    load_capability_catalog,
    load_inline_text_files,
    load_interpreter_system_skill,
)
from skillmind.skills.source_documents import validate_source_documents
from skillmind.skills.task_catalog import resolve_task_run_from_manifest
from tests.skills.manifest_gate_fixtures import directory_gate_source

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


@pytest.fixture
def interpreted(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """一般 source へ具体的な表・列・保存規則を追加し、意図的に古い要約を返す。"""

    source = tmp_path / "source"
    shutil.copytree(ROOT / "backend/tests/fixtures/skills/repository-review", source)
    with (source / "SKILL.md").open("a", encoding="utf-8") as stream:
        stream.write(
            "\nRegister `audit.review_run` first. Update `verdict` and `review_json` "
            "in the same row. Follow [storage](references/storage.md).\n"
        )
    (source / "references").mkdir(exist_ok=True)
    (source / "references/storage.md").write_bytes(
        b"# Storage\r\nSave FAIL only to `reviews/{runId}/{documentId}/result.json`.\r\n"
    )
    package = SkillPackageParser().parse_directory(source)
    files = load_inline_text_files(source, package)
    request = build_interpreter_request(
        package=package, source_files=files,
        analysis=SkillStaticAnalyzer().analyze(package, files),
        catalog=load_capability_catalog(CONTRACTS / "examples/skill-capability-catalog.v1.json"),
        system_skill=load_interpreter_system_skill(ROOT / "skills/skillmind-skill-interpreter"),
    )
    response = json.loads((CONTRACTS / "examples/skill-interpreter-response.v1.json").read_text())
    assert "audit.review_run" not in canonical_json(response)
    manifest = InterpreterFixtureRunner(CONTRACTS).run(
        request, response, bind_identity=True,
    )["runtime_manifest_draft"]
    return request, manifest, source


@pytest.mark.parametrize("segment_no", [1, 2])
def test_source_survives_summary_omission_publication_and_runtime(
    interpreted: tuple[dict[str, Any], dict[str, Any], Path], segment_no: int,
) -> None:
    """初回・承認後の双方へ、公開 source と同じ全本文・参照を権限追加なく渡す。"""

    request, manifest, source = interpreted
    before = deepcopy(manifest)
    design = directory_gate_source(manifest, source)
    validate_skill_design(source=design, contracts_dir=CONTRACTS)
    task = manifest["tasks"][0]
    resolved = resolve_task_run_from_manifest(
        skill_id=uuid4(), skill_version_id=uuid4(), skill_key=design.skill_key,
        version="1.0.0", manifest_checksum=design.manifest_checksum,
        manifest=manifest, task_key=task["key"],
    )
    assert resolved is not None
    frozen = resolved.skill_snapshot["manifest"]
    task_snapshot = {
        "task_key": task["key"], "capability": task["capability"],
        "skill_version_id": resolved.skill_snapshot["skill_version_id"],
        "manifest_checksum": design.manifest_checksum,
        "output_schema_checksum": "sha256:" + "a" * 64,
    }
    compiled = build_agent_task_brief(
        run_id=uuid4(), task_snapshot=task_snapshot, manifest=frozen, selected_sources={},
        tools=(),
        limits=RunLimits(max_turns=20, wall_timeout_seconds=900, max_output_bytes=1048576),
        segment_no=segment_no,
    )
    brief = compiled.brief
    Draft202012Validator(json.loads(
        (CONTRACTS / "agent-task-brief/v1.schema.json").read_text()
    )).validate(brief)
    assert brief["source_documents"] == request["source"]["source_documents"]
    prompt = render_task_brief_prompt(brief, input_json={}, output_schema={"type": "object"})
    assert canonical_json(request["source"]["source_documents"]) in prompt
    for literal in ("audit.review_run", "review_json", "reviews/{runId}/{documentId}/result.json"):
        assert literal in prompt
    assert "do not guess, pluralize, translate or rename identifiers" in prompt
    assert "existing approval/effect protocol" in prompt
    assert brief["allowed_tools"] == []
    assert manifest == before
    brief["source_documents"][0]["content"] = "changed"
    assert manifest == before


@pytest.mark.parametrize("change", ["content", "omit", "forged_hash"])
def test_publication_rejects_source_loss_even_with_rehashed_manifest(
    interpreted: tuple[dict[str, Any], dict[str, Any], Path], change: str,
) -> None:
    """Manifest 自体の checksum が一致しても、導入した原文との相違を拒否する。"""

    _, manifest, source = interpreted
    if change == "omit":
        manifest["source_documents"].pop()
    else:
        document = manifest["source_documents"][0]
        document["content"] = "Different business constraints."
        if change == "forged_hash":
            document["sha256"] = "sha256:" + sha256_hex(document["content"])
    design = directory_gate_source(manifest, source)
    with pytest.raises(SkillDesignInvalidError):
        validate_skill_design(source=design, contracts_dir=CONTRACTS)


def test_model_cannot_replace_platform_source_documents(
    interpreted: tuple[dict[str, Any], dict[str, Any], Path],
) -> None:
    """Model が返した本文を信用せず、原 request だけを再束縛する。"""

    request, manifest, _ = interpreted
    response = json.loads((CONTRACTS / "examples/skill-interpreter-response.v1.json").read_text())
    response["runtime_manifest_draft"]["source_documents"] = [{"content": "invented"}]
    validated = InterpreterFixtureRunner(CONTRACTS).run(request, response, bind_identity=True)
    assert validated["runtime_manifest_draft"]["source_documents"] == manifest["source_documents"]
    schema = build_interpreter_generation_schema(CONTRACTS)
    assert "source_documents" not in schema["properties"]
    assert "runtime_manifest_draft" not in schema["properties"]


def test_request_rejects_missing_reference_text(
    interpreted: tuple[dict[str, Any], dict[str, Any], Path],
) -> None:
    """Source index にある参照本文を欠く request を成功結果へ変換しない。"""

    request, _, _ = interpreted
    request["source"]["source_documents"].pop()
    response = json.loads((CONTRACTS / "examples/skill-interpreter-response.v1.json").read_text())
    with pytest.raises(ValueError, match="differ from the source index"):
        InterpreterFixtureRunner(CONTRACTS).run(request, response, bind_identity=True)


def test_runtime_rejects_damaged_source_text(
    interpreted: tuple[dict[str, Any], dict[str, Any], Path],
) -> None:
    """本文 hash の不一致を切捨てや要約で隠さない。"""

    _, manifest, _ = interpreted
    manifest["source_documents"][0]["content"] += "tampered"
    with pytest.raises(ValueError, match="integrity"):
        validate_source_documents(manifest["source_documents"])


@pytest.mark.parametrize("content", ["first\nsecond\n", "first\r\nsecond\r\n", ""])
def test_source_trace_line_boundary_uses_original_text_and_preserves_optional_line(
    content: str,
) -> None:
    """末尾改行・CRLF を架空の追加行と数えず、行未指定は元仕様どおり許可する。"""

    from skillmind.skills.source_documents import SourceTraceLocationError, validate_source_location

    files = {"source.md": content, "asset.bin": None}
    last_line = max(1, len(content.splitlines()))
    assert validate_source_location(
        "source.md", last_line, files, pointer="/source_traces/0",
    ) == "TEXT_SNAPSHOT"
    with pytest.raises(SourceTraceLocationError) as captured:
        validate_source_location("source.md", last_line + 1, files, pointer="/source_traces/0")
    assert captured.value.code == "source_trace_line_invalid"
    assert captured.value.path == "/source_traces/0/line"
    assert validate_source_location(
        "source.md", None, files, pointer="/source_traces/0",
    ) == "TEXT_SNAPSHOT"
    assert validate_source_location(
        "asset.bin", None, files, pointer="/source_traces/0",
    ) == "SOURCE_INDEX"
    with pytest.raises(SourceTraceLocationError):
        validate_source_location("asset.bin", 1, files, pointer="/source_traces/0")
