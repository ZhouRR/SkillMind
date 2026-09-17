"""Deterministic Skill importer の安全性、正規化、draft 生成を検証する。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from skillmind.integrations.domain import PROVIDER_DEFINITIONS
from skillmind.skills import (
    CapabilityCatalogEntry,
    CapabilityCatalogSnapshot,
    DeterministicManifestDraftBuilder,
    InlineSkillFile,
    InterpreterFixtureRunner,
    NormalizedSkillPackage,
    SkillImportError,
    SkillImportLimits,
    SkillPackageParser,
    SkillStaticAnalysis,
    SkillStaticAnalyzer,
    UnsafeSkillSourceError,
    build_interpreter_request,
    load_capability_catalog,
    load_inline_text_files,
    load_interpreter_system_skill,
)
from skillmind.skills.interpreter_cli import run_fixture
from skillmind.skills.manifest_gate import ManifestValidator
from tests.skills.manifest_gate_fixtures import directory_gate_source

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
TEST_SKILLS = Path(__file__).resolve().parents[1] / "fixtures/skills"
GENERIC_SKILL = TEST_SKILLS / "repository-review"
UNSAFE_SKILL = TEST_SKILLS / "unsafe-shell"
UNSAFE_CREDENTIAL = TEST_SKILLS / "unsafe-credential"
SYSTEM_SKILL = ROOT / "skills" / "skillmind-skill-interpreter"


def _write(path: Path, content: str | bytes) -> None:
    """Test source root 下の fixture file を必要 directory 付きで作成する。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _directory_skill(root: Path) -> None:
    """Front matter、reference、script、asset を持つ最小 directory Skill を作成する。"""

    _write(
        root / "SKILL.md",
        """---
name: Ticket Reviewer
description: Review one synthetic ticket.
argument-hint: "[ticket-id]"
allowed-tools: [Read, Grep]
custom-owner: quality-team
---
# Ticket Reviewer

Follow the deterministic checklist in [rules](references/rules.md).

## Steps

Do not execute bundled scripts during import.
""",
    )
    _write(root / "references" / "rules.md", "# Rules\n\nUse evidence.\n")
    _write(root / "scripts" / "check.py", "print('must not execute')\n")
    _write(root / "assets" / "icon.bin", b"\x00\x01")


def test_directory_skill_is_normalized_without_authorizing_tools(tmp_path: Path) -> None:
    """Known metadata/resource を取り込み、source Tool 声明を権限と分離する。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _directory_skill(root)

    package = SkillPackageParser().parse_directory(root)

    assert package.detected_adapter == "directory-skill/v1"
    assert package.name == "Ticket Reviewer"
    assert package.argument_hint == "[ticket-id]"
    assert package.declared_tools == ("Read", "Grep")
    assert package.extensions == {"custom-owner": "quality-team"}
    assert package.resources.scripts == ("scripts/check.py",)
    assert package.resources.references == ("references/rules.md",)
    assert package.resources.assets == ("assets/icon.bin",)
    assert package.files[-1].path == "scripts/check.py"
    assert package.diagnostics[0].code == "declared_tools_not_authorized"
    assert "must not execute" not in package.instruction_text


def test_source_and_normalized_hashes_are_deterministic(tmp_path: Path) -> None:
    """同一 file 集合は更新時刻に依存せず同じ hash を生成する。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _directory_skill(root)
    parser = SkillPackageParser()

    first = parser.parse_directory(root)
    (root / "SKILL.md").touch()
    second = parser.parse_directory(root)

    assert first.content_hash == second.content_hash
    assert first.to_dict() == second.to_dict()


def test_generic_markdown_becomes_assisted_candidate(tmp_path: Path) -> None:
    """SKILL.md のない document directory を executable にせず候補として保持する。"""

    root = (tmp_path / "docs").resolve()
    root.mkdir()
    _write(root / "README.md", "# Operations Guide\n\nExplain the manual process.\n")

    package = SkillPackageParser().parse_directory(root)

    assert package.detected_adapter == "generic-document/v1"
    assert package.name == "Operations Guide"
    assert package.diagnostics[0].code == "generic_document_assisted_only"


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("../outside.md", "reference_escape"),
        ("references/missing.md", "missing_reference"),
    ],
)
def test_markdown_reference_must_stay_inside_source(tmp_path: Path, target: str, code: str) -> None:
    """Root 逃逸と欠落参照を Adapter 選択前に拒否する。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _write(root / "SKILL.md", f"# Skill\n\nRead [target]({target}).\n")

    with pytest.raises(SkillImportError) as failure:
        SkillPackageParser().parse_directory(root)

    assert failure.value.code == code


def test_markdown_reference_cycle_is_rejected(tmp_path: Path) -> None:
    """相互参照を Interpreter で無限展開する前に検出する。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _write(root / "SKILL.md", "# Skill\n\nSee [A](references/a.md).\n")
    _write(root / "references" / "a.md", "See [root](../SKILL.md).\n")

    with pytest.raises(SkillImportError) as failure:
        SkillPackageParser().parse_directory(root)

    assert failure.value.code == "reference_cycle"


def test_yaml_alias_is_rejected(tmp_path: Path) -> None:
    """YAML alias による object expansion を front matter parse 前に拒否する。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _write(
        root / "SKILL.md",
        "---\nname: &name Skill\ndescription: *name\n---\n# Skill\n",
    )

    with pytest.raises(SkillImportError) as failure:
        SkillPackageParser().parse_directory(root)

    assert failure.value.code == "invalid_front_matter"


def test_import_limits_reject_oversized_file(tmp_path: Path) -> None:
    """File 数が少なくても単 file 上限を超える source を拒否する。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _write(root / "SKILL.md", "# " + ("x" * 100))
    parser = SkillPackageParser(
        limits=SkillImportLimits(
            max_files=10,
            max_file_bytes=32,
            max_total_bytes=1_000,
            max_markdown_bytes=32,
        )
    )

    with pytest.raises(SkillImportError) as failure:
        parser.parse_directory(root)

    assert failure.value.code == "file_too_large"


def test_deterministic_manifest_draft_is_assisted_and_schema_valid(tmp_path: Path) -> None:
    """Parser が業務契約を推測せず、有効な Assisted draft を決定的に返す。"""

    root = (tmp_path / "skill").resolve()
    root.mkdir()
    _directory_skill(root)
    package = SkillPackageParser().parse_directory(root)
    schema = json.loads(
        (ROOT / "contracts" / "runtime-manifest" / "v1alpha1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    builder = DeterministicManifestDraftBuilder(schema)

    first = builder.build(package)
    second = builder.build(package)

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(first)
    assert first == second
    assert first["compatibility"]["level"] == "assisted"
    assert first["tools"] == []
    assert first["extensions"]["declared_tools"] == ["Read", "Grep"]
    assert first["identity"]["interpreter_version"] == "deterministic-parser/1.0.0"


def _load_contract(relative: str) -> dict[str, object]:
    """Repository contract/example を JSON object として読み込む。"""

    value = json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package_and_analysis() -> tuple[NormalizedSkillPackage, SkillStaticAnalysis]:
    """Generic fixture の normalized package と static analysis を生成する。"""

    package = SkillPackageParser().parse_directory(GENERIC_SKILL.resolve())
    analysis = SkillStaticAnalyzer().analyze(
        package,
        load_inline_text_files(GENERIC_SKILL, package),
    )
    return package, analysis


def _interpreter_request() -> dict[str, object]:
    """Frozen catalog/system Skill を含む実際の Interpreter request を生成する。"""

    package, analysis = _package_and_analysis()
    return build_interpreter_request(
        package=package,
        source_files=load_inline_text_files(GENERIC_SKILL, package),
        analysis=analysis,
        catalog=load_capability_catalog(
            CONTRACTS / "examples" / "skill-capability-catalog.v1.json"
        ),
        system_skill=load_interpreter_system_skill(SYSTEM_SKILL),
    )


def test_static_analysis_is_deterministic_and_contract_valid() -> None:
    """Source 内容を実行せず、同一 index から同じ安全 report を生成する。"""

    package = SkillPackageParser().parse_directory(GENERIC_SKILL.resolve())
    files = load_inline_text_files(GENERIC_SKILL, package)
    first = SkillStaticAnalyzer().analyze(package, files)
    second = SkillStaticAnalyzer().analyze(package, files)
    schema = _load_contract("skills/interpreter/v1/static-analysis.schema.json")

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(first.to_dict())
    assert first == second
    assert first.blocked is False
    assert first.declared_tools == ("repository.read/v1",)
    assert first.checksum == _load_contract("examples/skill-static-analysis.v1.json")["checksum"]


def test_static_analysis_records_declared_builtin_tool_without_blocking() -> None:
    """内蔵 Tool の宣言は重表達信号として記録し、解釈は閉じない(source command も複製しない)。"""

    package = SkillPackageParser().parse_directory(UNSAFE_SKILL.resolve())
    analysis = SkillStaticAnalyzer().analyze(
        package,
        load_inline_text_files(UNSAFE_SKILL, package),
    )

    assert analysis.blocked is False
    assert {item.code for item in analysis.diagnostics} >= {
        "declared_builtin_tool",
        "shell_commands_require_manual_mapping",
    }
    assert "generate-output" not in json.dumps(analysis.to_dict())
    # 宣言だけでは model 呼び出し前に閉じない。
    request = build_interpreter_request(
        package=package,
        source_files=load_inline_text_files(UNSAFE_SKILL, package),
        analysis=analysis,
        catalog=load_capability_catalog(
            CONTRACTS / "examples" / "skill-capability-catalog.v1.json"
        ),
        system_skill=load_interpreter_system_skill(SYSTEM_SKILL),
    )
    assert request is not None


def test_static_analysis_blocks_credential_source_before_model() -> None:
    """平文 credential を含む source は model 呼び出し前に block する。"""

    package = SkillPackageParser().parse_directory(UNSAFE_CREDENTIAL.resolve())
    analysis = SkillStaticAnalyzer().analyze(
        package,
        load_inline_text_files(UNSAFE_CREDENTIAL, package),
    )

    assert analysis.blocked is True
    assert {item.code for item in analysis.diagnostics} >= {"credential_literal_not_allowed"}
    with pytest.raises(UnsafeSkillSourceError):
        build_interpreter_request(
            package=package,
            source_files=load_inline_text_files(UNSAFE_CREDENTIAL, package),
            analysis=analysis,
            catalog=load_capability_catalog(
                CONTRACTS / "examples" / "skill-capability-catalog.v1.json"
            ),
            system_skill=load_interpreter_system_skill(SYSTEM_SKILL),
        )


def test_static_analysis_rejects_content_changed_after_normalization() -> None:
    """Parser 後の file 差し替えを hash/size binding で拒否する。"""

    package = SkillPackageParser().parse_directory(GENERIC_SKILL.resolve())
    files = list(load_inline_text_files(GENERIC_SKILL, package))
    files[0] = InlineSkillFile(path=files[0].path, content=files[0].content + "\nchanged")

    with pytest.raises(ValueError, match="differs"):
        SkillStaticAnalyzer().analyze(package, files)


def test_capability_catalog_is_sorted_unique_and_checksum_bound() -> None:
    """Catalog 順序を固定し、重複 capability と checksum drift を拒否する。"""

    loaded = load_capability_catalog(CONTRACTS / "examples" / "skill-capability-catalog.v1.json")
    assert [item.capability for item in loaded.capabilities] == [
        "change.propose/v1",
        "database.read/v1",
        "database.write/v1",
        "document.convert/v1",
        "document.inspect/v1",
        "document.list/v1",
        "document.read/v1",
        "document.readiness/v1",
        "document.write/v1",
        "interaction.request/v1",
        "issue.read/v1",
        "issue.update/v1",
        "json.schema.validate/v1",
        "mcp.call/v1",
        "mcp.query/v1",
        "mcp.read/v1",
        "mcp.tools/v1",
        "repository.read/v1",
        "repository.write/v1",
        "subagent.dispatch/v1",
        "workspace.read/v1",
        "workspace.search/v1",
        "workspace.write/v1",
        "workspace.write/v2",
    ]
    issue = next(item for item in loaded.capabilities if item.capability == "issue.read/v1")
    assert issue.providers == ("redmine",)
    assert set(issue.providers) == {
        provider
        for provider, definition in PROVIDER_DEFINITIONS.items()
        if definition.installed and issue.capability in definition.capabilities
    }
    assert loaded.checksum == (
        "sha256:34fda26a4b4ea0541ce12317c3334be850903c77642d9e7ec768bcafb629ccea"
    )
    duplicate = CapabilityCatalogEntry(
        capability="issue.read/v1",
        description="Read issue",
        request_schema="request.schema.json",
        response_schema="response.schema.json",
        error_schema="error.schema.json",
        providers=("csv",),
    )
    with pytest.raises(ValueError, match="Duplicate"):
        CapabilityCatalogSnapshot.build(
            catalog_version="test/v1", capabilities=(duplicate, duplicate)
        )


def test_system_skill_identity_is_versioned_and_matches_fixture_contract() -> None:
    """System Skill の source/prompt checksum と SemVer を request identity に固定する。"""

    identity = load_interpreter_system_skill(SYSTEM_SKILL)
    example = _load_contract("examples/skill-interpreter-request.v1.json")["interpreter"]

    assert identity.version == "6.0.0"
    assert identity.interpreter_version == "skillmind-skill-interpreter/6.0.0"
    assert identity.to_dict() == example


def _system_prompt() -> str:
    """本番と同じ loader で本文と参照文書を一緒に読む。"""

    package = SkillPackageParser().parse_directory(SYSTEM_SKILL.resolve())
    return " ".join(
        " ".join(item.content.split())
        for item in load_inline_text_files(SYSTEM_SKILL, package)
    )


def test_system_skill_preserves_business_language_and_source_constraints() -> None:
    """Schema だけでは検出できない原文保持と手順変換の境界を prompt に残す。"""

    prompt = _system_prompt()
    for instruction in (
        'source language',
        'complete original Skill and references',
        'Exact table names, columns, paths, conditions and processing order '
        'remain in the original source',
        'Do not rewrite business procedures',
    ):
        assert instruction in prompt


def test_system_skill_maps_catalog_capabilities_without_granting_writes() -> None:
    """利用可能な Git write を旧説明で抑止せず、承認と Provider 境界を保持する。"""

    prompt = _system_prompt()
    for instruction in (
        'registered capabilities and supported Providers',
        'Source commands and bundled scripts do not grant execution permission',
        'automatic approval option govern actual proposals',
        'workspace.write/v2',
    ):
        assert instruction in prompt


def test_system_skill_separates_resource_setup_from_document_execution() -> None:
    """条件付き write の任意接続化と登録前メタデータの循環依存を防ぐ指示を固定する。"""

    prompt = _system_prompt()
    for instruction in (
        'Group targets sharing a connection and authorization purpose',
        'its availability does not approve or force a write',
        'Document selection and resource binding already supply',
        'metadata is not document content',
    ):
        assert instruction in prompt


def test_system_skill_bounds_contracts_without_changing_source_deliverables() -> None:
    """フォーム深度を守り、Markdown や深い成果物を不要な出力契約へ押し込まない。"""

    prompt = _system_prompt()
    for instruction in (
        'Declare only values the caller must choose',
        'Use an empty object contract when no caller input is needed',
        'Do not rewrite business procedures, rules, output schemas',
        'platform owns task keys',
    ):
        assert instruction in prompt


def test_fixture_runner_validates_response_and_publishable_manifest() -> None:
    """Model 不使用 fixture を response/report/Manifest と publish gate で検証する。"""

    request = _interpreter_request()
    response = _load_contract("examples/skill-interpreter-response.v1.json")
    validated = InterpreterFixtureRunner(CONTRACTS).run(request, response)
    manifest = validated["runtime_manifest_draft"]
    assert isinstance(manifest, dict)
    passed, findings = ManifestValidator(CONTRACTS).evaluate(
        directory_gate_source(manifest, GENERIC_SKILL)
    )

    assert passed is True
    assert findings == ()


def test_bind_identity_stamps_platform_identity_on_model_output() -> None:
    """bind_identity=True は identity を platform 権威値で上書きし、既定は従来どおり照合する。"""

    request = _interpreter_request()
    response = _load_contract("examples/skill-interpreter-response.v1.json")
    # model が interpreter_version を平台の合成形式ではない値で埋めた状況を再現する。
    response["runtime_manifest_draft"]["identity"]["interpreter_version"] = "2.1.0"  # type: ignore[index]

    # fixture 経路(既定)は従来どおり mismatch を拒否し、防串源を保つ。
    with pytest.raises(ValueError, match="interpreter version does not match"):
        InterpreterFixtureRunner(CONTRACTS).run(request, response)

    # model 経路は contract draft を受け取り、platform 権威の identity と Schema を stamp する。
    generated = _load_contract("examples/generated-task-manifest.v1alpha1.json")
    generated_tasks = generated["tasks"]
    assert isinstance(generated_tasks, list)
    task = generated_tasks[0]
    assert isinstance(task, dict)
    for key in (
        "input_schema",
        "output_schema",
        "input_schema_checksum",
        "output_schema_checksum",
    ):
        task.pop(key)
    response["runtime_manifest_draft"]["tasks"] = [task]  # type: ignore[index]
    validated = InterpreterFixtureRunner(CONTRACTS).run(request, response, bind_identity=True)
    identity = validated["runtime_manifest_draft"]["identity"]
    assert identity["interpreter_version"] == "skillmind-skill-interpreter/6.0.0"
    assert identity["source_hash"] == request["source"]["content_hash"]  # type: ignore[index]
    # 蓝图は同じ解釈の一部であり、manifest と別の identity/互換 level を持ってはならない。
    blueprint = validated["runtime_manifest_draft"]["capability_blueprint"]
    assert blueprint["identity"] == identity
    assert (
        blueprint["compatibility"]["level"]
        == (validated["runtime_manifest_draft"]["compatibility"]["level"])
    )
    compiled_task = validated["runtime_manifest_draft"]["tasks"][0]
    assert compiled_task["input_schema_checksum"] == (
        "sha256:80d83714d6fd98b37626acec9bfe7c9ba8dc749812549f523c01a02e1281f75c"
    )


def test_model_path_accepts_open_outcome_without_output_contract() -> None:
    """開放式 report の model 解釈は出力 draft を捏造せず発行候補へ変換できる。"""

    request = _interpreter_request()
    response = _load_contract("examples/skill-interpreter-response.v1.json")
    task = response["runtime_manifest_draft"]["tasks"][0]  # type: ignore[index]
    assert isinstance(task, dict)
    for key in (
        "output_contract",
        "output_schema",
        "output_schema_checksum",
    ):
        task.pop(key)
    traces = task["contract_source_trace"]
    assert isinstance(traces, list)
    task["contract_source_trace"] = [item for item in traces if item["contract"] == "input"]

    validated = InterpreterFixtureRunner(CONTRACTS).run(request, response, bind_identity=True)
    compiled = validated["runtime_manifest_draft"]["tasks"][0]

    assert "output_contract" not in compiled
    assert "output_schema" not in compiled
    assert "output_schema_checksum" not in compiled


def test_model_path_rejects_legacy_contract_references() -> None:
    """新規 model 解釈が Release A の business Schema 参照へ戻らないことを保証する。"""

    request = _interpreter_request()
    response = _load_contract("examples/skill-interpreter-response.v1.json")
    task = response["runtime_manifest_draft"]["tasks"][0]  # type: ignore[index]
    assert isinstance(task, dict)
    task.pop("input_contract")
    task.pop("output_contract")
    task["input_schema"] = {"$ref": "legacy/input.schema.json"}
    task["output_schema"] = {"$ref": "legacy/output.schema.json"}

    with pytest.raises(ValueError, match="must define input_contract"):
        InterpreterFixtureRunner(CONTRACTS).run(request, response, bind_identity=True)


def test_fixture_manifest_gate_rejects_generated_schema_drift() -> None:
    """Interpreter draft の Generated Schema 改変を publish gate が拒否する。"""

    response = _load_contract("examples/skill-interpreter-response.v1.json")
    manifest = response["runtime_manifest_draft"]
    assert isinstance(manifest, dict)
    tasks = manifest["tasks"]
    assert isinstance(tasks, list)
    task = tasks[0]
    assert isinstance(task, dict)
    task["input_schema"]["additionalProperties"] = True

    passed, findings = ManifestValidator(CONTRACTS).evaluate(
        directory_gate_source(manifest, GENERIC_SKILL)
    )

    assert passed is False
    assert "task_contract_schema_mismatch" in {item.code for item in findings}


def test_fixture_runner_rejects_schema_and_snapshot_drift() -> None:
    """追加 field と catalog checksum 改変を model adapter 前後で fail closed にする。"""

    runner = InterpreterFixtureRunner(CONTRACTS)
    response = _load_contract("examples/skill-interpreter-response.v1.json")
    invalid_response = copy.deepcopy(response)
    invalid_response["unexpected"] = True
    with pytest.raises(ValidationError):
        runner.run(_interpreter_request(), invalid_response)

    invalid_request = _interpreter_request()
    catalog = invalid_request["capability_catalog"]
    assert isinstance(catalog, dict)
    catalog["catalog_version"] = "tampered"
    with pytest.raises(ValueError, match="checksum"):
        runner.run(invalid_request, response)


def test_offline_fixture_runner_uses_repository_assets() -> None:
    """CLI と共有する offline path が model credential なしで完結する。"""

    result = run_fixture(
        source=GENERIC_SKILL,
        fixture_response=CONTRACTS / "examples" / "skill-interpreter-response.v1.json",
        contracts_dir=CONTRACTS,
        system_skill=SYSTEM_SKILL,
        capability_catalog=CONTRACTS / "examples" / "skill-capability-catalog.v1.json",
    )

    response = result["response"]
    assert isinstance(response, dict)
    assert response["response_version"] == "skillmind.skill-interpreter.response/v1"


def test_system_skill_keeps_parallel_execution_optional_and_bounded() -> None:
    """独立した観点だけで扇出を強制せず、依存順と読取専用の境界を保つ。"""

    prompt = _system_prompt()
    for instruction in ('only needed task-local capabilities', 'platform creates one task'):
        assert instruction in prompt


def test_system_skill_preserves_mandatory_effect_before_document_access() -> None:
    """原文依拠の gate、按需準備と不明結果の扱いを prompt に固定する。"""

    prompt = _system_prompt()
    for instruction in (
        'processing order remain in the original source',
        'required document conversion explicitly',
        'Platform approvals',
    ):
        assert instruction in prompt


def test_system_skill_separates_blueprint_and_manifest_source_trace_scopes() -> None:
    """出典の target 基準点を区別し、Tool の trace を Blueprint に混入させない。"""

    prompt = _system_prompt()
    for instruction in (
        'Source references use the supplied source ID',
        'Never invent file IDs or line numbers',
        'Input and resource declarations require a source reference',
    ):
        assert instruction in prompt
