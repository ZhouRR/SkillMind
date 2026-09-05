"""S5 泛化验收: 通用 fixture と去 JAF 化判据を model なしで検証する。

Generic Native / Generic Adapted / Unsafe / JAF source の四類をオフラインで通し、
通用モジュールが JAF 固有の dispatch 定数を持たないことを守る。
モデル反復メトリクスと実機 SUCCEEDED は PostgreSQL/ANTHROPIC を要する残余。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from projectmind.skills import (
    InlineSkillFile,
    SkillPackageParser,
    SkillService,
    SkillStaticAnalyzer,
    UnsafeSkillSourceError,
    build_interpreter_request,
    load_capability_catalog,
    load_inline_text_files,
    load_interpreter_system_skill,
)
from projectmind.skills.interpreter import InterpreterFixtureRunner
from projectmind.skills.manifest_gate import ManifestValidator
from projectmind.skills.task_catalog import (
    project_published_tasks,
    resolve_task_run_from_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
GENERIC_SKILL = ROOT / "skills" / "examples" / "repository-review"
REPOSITORY_REVIEW_SKILL = GENERIC_SKILL
UNSAFE_SKILL = ROOT / "skills" / "examples" / "unsafe-shell"
UNSAFE_CREDENTIAL = ROOT / "skills" / "examples" / "unsafe-credential"
JAF_SKILL = ROOT / "skills" / "examples" / "jaf-ticket-quality"
INSUFFICIENT_SKILL = ROOT / "skills" / "examples" / "insufficient-contract" / "SKILL.md"
SYSTEM_SKILL = ROOT / "skills" / "projectmind-skill-interpreter"
CATALOG = CONTRACTS / "examples" / "skill-capability-catalog.v1.json"
NATIVE_MANIFEST = CONTRACTS / "examples" / "generic-native-manifest.v1alpha1.json"
RESPONSE = CONTRACTS / "examples" / "skill-interpreter-response.v1.json"

# 通用実行/投影モジュールは JAF 固有の dispatch 定数を持ってはならない: 計画 §4。
GENERIC_MODULES = (
    ROOT / "backend" / "src" / "projectmind" / "skills" / "task_catalog.py",
    ROOT / "backend" / "src" / "projectmind" / "agent" / "context_builder.py",
    ROOT / "backend" / "src" / "projectmind" / "agent" / "result_validation.py",
)
JAF_DISPATCH_CONSTANTS = ("jaf.ticket.analyze", "fixture-001")


def _load(path: Path) -> dict[str, object]:
    """Contract JSON を object として読み込む。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _analyze(root: Path) -> tuple[object, object]:
    """Fixture directory を正規化し、静的解析まで実行する。"""

    package = SkillPackageParser().parse_directory(root.resolve())
    analysis = SkillStaticAnalyzer().analyze(package, load_inline_text_files(root, package))
    return package, analysis


def test_generic_native_manifest_passes_gate_and_projects_task() -> None:
    """完整 Native Manifest はモデルなしで gate を通り、task 発見と Run 解決ができる。"""

    manifest = _load(NATIVE_MANIFEST)
    identity = manifest["identity"]
    assert isinstance(identity, dict)
    passed, findings = ManifestValidator(CONTRACTS).evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),
        interpretation_id=UUID(str(identity["interpretation_id"])),
    )

    assert passed is True
    assert findings == ()
    assert manifest["compatibility"] == {"level": "native", "confidence": 1.0, "diagnostics": []}

    descriptors = project_published_tasks(
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="generic-native-review",
        skill_name="Generic Native Review",
        version="1.0.0",
        published_at=None,
        manifest=manifest,
    )
    assert [(item.task_key, item.capability) for item in descriptors] == [
        ("review-file", "repository.review")
    ]

    resolved = resolve_task_run_from_manifest(
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="generic-native-review",
        version="1.0.0",
        manifest_checksum="sha256:" + ("b2" * 32),
        manifest=manifest,
        task_key="review-file",
    )
    assert resolved is not None
    assert resolved.allowed_capabilities == ("repository.read/v1",)


def test_generic_adapted_source_becomes_publishable_after_interpretation() -> None:
    """Adapted 源は静的解析を通過し、fixture 応答が発行可能 Manifest へ検証される。"""

    package, analysis = _analyze(GENERIC_SKILL)
    assert analysis.blocked is False

    request = build_interpreter_request(
        package=package,
        analysis=analysis,
        catalog=load_capability_catalog(CATALOG),
        system_skill=load_interpreter_system_skill(SYSTEM_SKILL),
    )
    validated = InterpreterFixtureRunner(CONTRACTS).run(request, _load(RESPONSE))
    manifest = validated["runtime_manifest_draft"]
    assert isinstance(manifest, dict)

    passed, findings = ManifestValidator(CONTRACTS).evaluate(
        manifest,
        source_hash=str(_load(RESPONSE)["source_hash"]),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000301"),
    )
    assert passed is True
    assert findings == ()


def test_same_fixture_interpretation_is_structurally_stable_three_times() -> None:
    """同一 frozen 入力を三回処理して Schema 有効率と意味構造が 3/3 一致する。"""

    package, analysis = _analyze(GENERIC_SKILL)
    request = build_interpreter_request(
        package=package,
        analysis=analysis,
        catalog=load_capability_catalog(CATALOG),
        system_skill=load_interpreter_system_skill(SYSTEM_SKILL),
    )
    response = _load(RESPONSE)
    outputs = [
        InterpreterFixtureRunner(CONTRACTS).run(
            request,
            deepcopy(response),
            bind_identity=True,
        )
        for _ in range(3)
    ]

    assert outputs[0] == outputs[1] == outputs[2]
    assert all(
        output["report"]["report_version"] == "projectmind.skill-interpretation-report/v1"
        for output in outputs
    )


def test_insufficient_source_stays_assisted_without_invented_fields() -> None:
    """字段信息不足的 Skill 保持可调整 Assisted Preview, 不伪造业务字段。"""

    preview = SkillService(None, CONTRACTS).preview_inline(  # type: ignore[arg-type]
        (
            InlineSkillFile(
                path="SKILL.md",
                content=INSUFFICIENT_SKILL.read_text(encoding="utf-8"),
            ),
        )
    )
    manifest = preview.runtime_manifest_draft
    task = manifest["tasks"][0]

    assert manifest["compatibility"]["level"] == "assisted"
    assert task["input_contract"]["fields"] == []
    assert task["output_contract"]["fields"] == []
    assert task["input_schema"]["properties"] == {}
    assert task["output_schema"]["properties"] == {}


def test_repository_review_sample_has_no_predefined_business_schema() -> None:
    """配布用 sample は SKILL.md と登録済み read Tool だけで業務契約を記述する。"""

    package, analysis = _analyze(REPOSITORY_REVIEW_SKILL)

    assert package.detected_adapter == "directory-skill/v1"
    assert package.name == "repository-review"
    assert package.primary_instruction == "SKILL.md"
    assert package.declared_tools == ("repository.read/v1",)
    assert {item.path for item in package.files} == {"SKILL.md", "agents/openai.yaml"}
    assert package.extensions == {}
    assert analysis.blocked is False
    assert analysis.declared_tools == ("repository.read/v1",)
    assert analysis.unsupported_operations == ()


def test_unsafe_source_is_blocked_before_model_and_not_publishable() -> None:
    """平文 credential を含む源は定位付き diagnostic で block され、モデル呼び出し前に閉じる。"""

    package, analysis = _analyze(UNSAFE_CREDENTIAL)

    assert analysis.blocked is True
    assert {item.code for item in analysis.diagnostics} >= {"credential_literal_not_allowed"}
    with pytest.raises(UnsafeSkillSourceError):
        build_interpreter_request(
            package=package,
            analysis=analysis,
            catalog=load_capability_catalog(CATALOG),
            system_skill=load_interpreter_system_skill(SYSTEM_SKILL),
        )


def test_declared_builtin_tool_is_reconciled_not_blocked() -> None:
    """内蔵 Tool の宣言は非ブロッキング信号に留め、解釈へ進める(声明 != 授与)。"""

    _, analysis = _analyze(UNSAFE_SKILL)

    assert analysis.blocked is False
    assert {item.code for item in analysis.diagnostics} >= {"declared_builtin_tool"}


def test_jaf_business_source_uses_the_same_safe_interpretation_entry() -> None:
    """JAF 业务说明也只作为 SKILL.md 输入, 不携带固定 Manifest 或 Schema asset。"""

    package, analysis = _analyze(JAF_SKILL)

    assert package.detected_adapter == "directory-skill/v1"
    assert {item.path for item in package.files} == {"SKILL.md"}
    assert package.declared_tools == ("issue.read/v1", "repository.read/v1")
    assert analysis.blocked is False


def test_generic_modules_have_no_jaf_dispatch_constants() -> None:
    """通用投影/実行モジュールが JAF 固有の dispatch 定数を持たないことを守る。"""

    for module in GENERIC_MODULES:
        source = module.read_text(encoding="utf-8")
        for constant in JAF_DISPATCH_CONSTANTS:
            assert constant not in source, f"{module.name} must not branch on {constant}"
