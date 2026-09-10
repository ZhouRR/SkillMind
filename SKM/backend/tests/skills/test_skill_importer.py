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
        "document.read/v1",
        "interaction.request/v1",
        "issue.read/v1",
        "issue.update/v1",
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
        "sha256:8b541cb757e6520c6237a7c5dca1557bde562196bb10fcbf75e4c7e103231ce3"
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

    assert identity.version == "4.0.0"
    assert identity.interpreter_version == "skillmind-skill-interpreter/4.0.0"
    assert identity.to_dict() == example


def test_system_skill_instructs_output_language_to_follow_the_source() -> None:
    """解釈結果の自然言語は Skill 源の言語に従い、識別子は ASCII に留まると指示する。

    この規則は model が実行するため確定性検査で守れない (docs/11 §5.1)。prompt から落ちても
    schema は通り、日本語 Skill が英語の蓝图を返すだけなので気付けない。唯一の防波堤として
    指示が prompt に載っていること自体をここで固定する。
    """

    package = SkillPackageParser().parse_directory(SYSTEM_SKILL.resolve())
    prompt = "\n".join(item.content for item in load_inline_text_files(SYSTEM_SKILL, package))

    assert "same natural language as the Skill source" in prompt
    assert "stay lowercase ASCII" in prompt
    assert "Add an `output_contract` only" in prompt
    assert "`change.propose/v1`" in prompt
    assert "For `mode=propose`" in prompt
    assert "without `change.propose/v1`" in prompt
    assert "Never declare an apply" in prompt
    assert "capability such as `issue.update/v1`" in prompt
    assert "For Git/SVN" in prompt
    assert "generic OutcomeEnvelope" in prompt


def test_system_skill_instructs_procedural_re_expression_of_raw_tool_steps() -> None:
    """生ツール手順は能力呼び出しへ重表達し、業務規則は原文保持すると指示する (計画 §21 I2)。

    静的門は I1 で「宣言は授与ではない」を実装したが、その先の重表達は model が行うため
    確定性検査で守れない。指示が prompt から落ちても schema は通り、蓝图に `svn cat` が
    原文のまま載るだけで気付けない。指示の在中自体をここで固定する (docs/11 §5.4)。
    """

    package = SkillPackageParser().parse_directory(SYSTEM_SKILL.resolve())
    prompt = "\n".join(item.content for item in load_inline_text_files(SYSTEM_SKILL, package))

    assert "## Procedural re-expression" in prompt
    # 三档: 直訳 / idiom 換え / guidance 降格。
    assert "**Direct.**" in prompt
    assert "**Different idiom.**" in prompt
    assert "**Guidance.**" in prompt
    # 過程は重表達、業務は原文保持という分界。
    assert "Re-express mechanism, preserve business" in prompt
    assert "keeps the source's own wording" in prompt
    # 守卫: catalog 登録済み かつ requirement 宣言済みの能力のみ。捏造禁止。
    assert "must already exist in the frozen capability catalog" in prompt
    assert "invent a capability identifier to make a step fit" in prompt
    # 声明・重表達のいずれも授与ではない。接続情報と credential は解釈へ持ち込まない。
    assert "Re-expressing a step grants nothing" in prompt
    assert "never an authorization" in prompt
    assert "Never copy a credential" in prompt
    # 硬失败は required 核心手順が三档いずれにも落ちない場合のみ。
    assert "Interpret rather than refuse" in prompt
    assert "never by itself such a case" in prompt


def test_system_skill_maps_procedure_onto_the_capabilities_that_now_exist() -> None:
    """idiom 档の写像先を §19/§20 で実装済みの能力へ更新したことを固定する (計画 §21 I4)。

    I2 の三档は当時の能力集で書かれており、commit 履歴・binary 設計書・仓库書き込みは
    「等価能力なし」として guidance へ降格する例だった。§19 W5 と §20 でそれらが実装された
    後もこの文面が残ると、model は**実行できる手順をわざわざ降格し続ける**。降格は静かに
    起きて schema も通るため、指示の在中をここで固定する以外に検知手段が無い。
    """

    package = SkillPackageParser().parse_directory(SYSTEM_SKILL.resolve())
    prompt = "\n".join(item.content for item in load_inline_text_files(SYSTEM_SKILL, package))

    # 名前での file 探索は物化索引 (§19 W5) へ写す。
    assert "files.txt" in prompt
    assert "`workspace.search/v1`" in prompt
    # commit 履歴は物化 history (§19 W5) の読取へ写す。もう guidance 降格ではない。
    assert "history.txt" in prompt
    assert "svn log" in prompt
    # binary 設計書は platform が text 化した副本 (§19 W5 / §20 R4a) を読む。
    assert "`.xlsx`, `.xlsm`, `.docx`" in prompt
    assert "<original name>.txt" in prompt
    # 中間産物の書き出しは workspace.write (§19 W2)。物化 input/ は書けない。
    assert "`workspace.write/v1`" in prompt
    assert "`workspace.write/v2`" in prompt
    assert "committed Tool response's `artifact_refs`" in prompt
    assert "never upgrade an existing published Skill or Run permission" in prompt
    assert "frozen evidence and is never" in prompt
    # 仓库への変更は直訳ではなく提案 (§20)。Agent は commit しない。
    assert "`svn commit`, `git commit`" in prompt
    assert "Agent proposes and never commits" in prompt
    # guidance 档に残るのは本当に等価能力が無いものだけ。
    assert "a format the platform cannot textualize" in prompt


def test_output_contract_scopes_without_invention_to_business_content() -> None:
    """`without invention` は業務内容のみを縛り、手順の重表達を禁じないと明記する。"""

    contract = (SYSTEM_SKILL / "references" / "output-contract.md").read_text(encoding="utf-8")

    assert "That rule binds business content" in contract
    assert "not procedure" in contract
    assert 'per `SKILL.md` "Procedural re-expression"' in contract
    assert "in the frozen catalog *and* in some `resource_requirements`" in contract
    assert "never appears in a step, rule, or deliverable" in contract


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
    assert identity["interpreter_version"] == "skillmind-skill-interpreter/4.0.0"
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


def test_system_skill_maps_independent_aspects_onto_bounded_fan_out() -> None:
    """扇出の档と、その適用境界が prompt に載っていることを固定する (計画 §23 D1/D5)。

    この規則は model が実行するため確定性検査では守れない。落ちても schema は通り、
    「独立に検討できる面」が順次実行へ黙って戻るだけなので気付けない。とくに
    「互いの結果に依存する手順は並行化しない」が落ちると、依存のある手順が並行化され、
    結論が実行順に左右されるようになる——再現しない不具合として現れる最悪の形。
    """

    package = SkillPackageParser().parse_directory(SYSTEM_SKILL.resolve())
    prompt = "\n".join(item.content for item in load_inline_text_files(SYSTEM_SKILL, package))

    assert "subagent.dispatch/v1" in prompt
    # 子は読み取り専用の真部分集合しか持たない。
    assert "read-only subset" in prompt
    # 入れ子禁止と、書き込み・問い合わせの不許可。
    assert "cannot fan out" in prompt
    assert "cannot write, cannot ask the user" in prompt
    # 依存のある手順は順次のまま。
    assert "those stay sequential" in prompt
