"""RuntimeManifest publish gate の hard deny 判定を検証する。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, ValidationError

from projectmind.skills import InlineSkillFile, SkillService
from projectmind.skills.domain import ManifestGateFinding
from projectmind.skills.manifest_gate import ManifestValidator
from projectmind.skills.runtime_defaults import normalize_runtime_manifest
from projectmind.skills.task_contract import (
    MAX_CONTRACT_FIELDS,
    MAX_DESCRIPTION_LENGTH,
    MAX_ENUM_VALUES,
    TASK_CONTRACT_VERSION,
    TaskContractCompilationError,
    compile_task_contract,
)

ROOT = Path(__file__).resolve().parents[3]


def _minimal_manifest() -> dict[str, object]:
    """Generated Task Contract を持つ最小 Manifest を返す。"""

    return _generated_manifest()


def test_minimal_generated_manifest_is_completed_and_passes_gate() -> None:
    """省略 field は安全な既定値で補い、Generated Schema で gate を通す。"""

    manifest = _minimal_manifest()
    normalized = normalize_runtime_manifest(manifest)
    identity = normalized["identity"]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),
        interpretation_id=UUID(str(identity["interpretation_id"])),
    )

    assert normalized["tasks"][0]["capability"] == "repository-review.review-file"
    assert normalized["tasks"][0]["view"] == "standard"
    assert normalized["permissions"]["external_write_policy"] == "deny"
    assert normalized["ui"] == {
        "default_view": "standard",
        "views": [],
        "frontend_module": None,
    }
    assert passed is True
    assert not [item for item in findings if item.severity == "error"]


def test_missing_custom_view_is_warning_with_standard_fallback() -> None:
    """Custom ViewSpec の欠落は Schema 実行を妨げず warning に留める。"""

    manifest = _minimal_manifest()
    manifest["tasks"][0]["view"] = "custom-report"  # type: ignore[index]
    identity = manifest["identity"]  # type: ignore[assignment]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),  # type: ignore[index]
        interpretation_id=UUID(str(identity["interpretation_id"])),  # type: ignore[index]
    )

    assert passed is True
    view_finding = next(item for item in findings if item.code == "view_fallback_required")
    assert view_finding.severity == "warning"


def _manifest_with_steps(
    steps: list[dict[str, str]], requirements: list[dict[str, object]]
) -> dict[str, object]:
    """重表達された手順と資源要求を差し替えた Manifest を返す (計画 §21 I3)。"""

    manifest = _generated_manifest()
    blueprint = manifest["capability_blueprint"]
    assert isinstance(blueprint, dict)
    blueprint["resource_requirements"] = requirements
    guidance = blueprint["guidance"]
    assert isinstance(guidance, dict)
    guidance["recommended_steps"] = steps
    return manifest


def _evaluate(manifest: dict[str, object]) -> tuple[bool, tuple[ManifestGateFinding, ...]]:
    """基準 identity で publish gate を実行する。"""

    identity = manifest["identity"]
    assert isinstance(identity, dict)
    return ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),
        interpretation_id=UUID(str(identity["interpretation_id"])),
    )


def _step_codes(findings: tuple[ManifestGateFinding, ...]) -> set[str]:
    """手順能力照合が出した finding code を取り出す。"""

    return {
        item.code
        for item in findings
        if item.code.startswith("capability_blueprint:step_capability")
    }


def test_re_expressed_step_capability_passes_when_registered_and_disclosed() -> None:
    """登録済みかつ資源要求が開示する能力への重表達は素通りする。"""

    manifest = _manifest_with_steps(
        [
            {
                "key": "read-target",
                "text": "対象ファイルを repository.read/v1 で読む。",
            }
        ],
        [
            {
                "key": "source-repository",
                "kind": "repository",
                "required": True,
                "access": "read",
                "capabilities": ["repository.read/v1"],
            }
        ],
    )

    passed, findings = _evaluate(manifest)

    assert passed is True
    assert _step_codes(findings) == set()


def test_step_naming_unregistered_capability_degrades_without_blocking_publish() -> None:
    """未登録能力を名指しした手順は warning で降格し、発行は止めない (D-A3)。

    ここを error にすると「対齐できなければ失敗」へ逆戻りし、写像できた残りの手順まで
    道連れになる。docs/11 §5.4 の三档は、落ちた一手順だけを降格させる。
    """

    manifest = _manifest_with_steps(
        [{"key": "read-history", "text": "変更履歴を repository.history/v1 で辿る。"}],
        [],
    )

    passed, findings = _evaluate(manifest)

    assert passed is True
    assert _step_codes(findings) == {"capability_blueprint:step_capability_unregistered"}
    finding = next(
        item
        for item in findings
        if item.code == "capability_blueprint:step_capability_unregistered"
    )
    assert finding.severity == "warning"
    assert finding.path == "/capability_blueprint/guidance/recommended_steps/0"


def test_step_capability_absent_from_requirements_is_reported() -> None:
    """登録済みでも資源要求が開示しない能力は、Run で束縛されないため報告する。"""

    manifest = _manifest_with_steps(
        [{"key": "read-issue", "text": "障害票を issue.read/v1 で取得する。"}],
        [],
    )

    passed, findings = _evaluate(manifest)

    assert passed is True
    assert _step_codes(findings) == {"capability_blueprint:step_capability_not_disclosed"}


def test_run_scoped_capability_in_step_needs_no_resource_requirement() -> None:
    """Run 内の既束縛 workspace を使う手順は資源要求を必要としない。"""

    manifest = _manifest_with_steps(
        [{"key": "find-file", "text": "workspace.search/v1 で対象ファイルを探す。"}],
        [],
    )

    passed, findings = _evaluate(manifest)

    assert passed is True
    assert _step_codes(findings) == set()


def test_url_in_step_is_not_matched_through_its_scheme() -> None:
    """scheme 付き URL は能力の名指しと取り違えない。

    scheme を持たない裸の host 断片 (`docs.example.com/v1`) は能力識別子と構文上区別できず
    未登録として報告される。接続情報は手順に現れてはならない (SKILL.md) ため、その報告は
    誤検出というより境界違反の指摘であり、warning 止まりで実害はない。
    """

    manifest = _manifest_with_steps(
        [{"key": "cite-doc", "text": "参照仕様は https://docs.example.com/v1 に記載されている。"}],
        [],
    )

    passed, findings = _evaluate(manifest)

    assert passed is True
    assert _step_codes(findings) == set()


def test_degraded_guidance_step_without_capability_reference_is_untouched() -> None:
    """能力を名指ししない降格 guidance は照合対象にならず、原文のまま残る。"""

    manifest = _manifest_with_steps(
        [
            {
                "key": "commit-history",
                "text": "SVN のコミット履歴の確認は、対応する能力が載るまで実行できない。",
            }
        ],
        [],
    )

    passed, findings = _evaluate(manifest)

    assert passed is True
    assert _step_codes(findings) == set()


def test_required_task_key_is_not_invented_by_defaults() -> None:
    """最低契約である Task key の欠落は補完せず hard error として残す。"""

    manifest = _minimal_manifest()
    del manifest["tasks"][0]["key"]  # type: ignore[index]
    identity = manifest["identity"]  # type: ignore[assignment]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),  # type: ignore[index]
        interpretation_id=UUID(str(identity["interpretation_id"])),  # type: ignore[index]
    )

    assert passed is False
    assert "manifest_schema_invalid" in {item.code for item in findings}


def test_optional_asset_diagnostic_cannot_create_a_hard_error() -> None:
    """旧分類の error でも任意 View/fixture は warning へ正規化する。"""

    manifest = _minimal_manifest()
    manifest["compatibility"] = {
        "level": "adapted",
        "diagnostics": [
            {
                "severity": "error",
                "code": "missing:view_spec",
                "message": "Referenced optional assets were not visible to the model",
            }
        ],
    }
    identity = manifest["identity"]  # type: ignore[assignment]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),  # type: ignore[index]
        interpretation_id=UUID(str(identity["interpretation_id"])),  # type: ignore[index]
    )

    assert passed is True
    finding = next(
        item for item in findings if item.code == "interpretation:missing:view_spec"
    )
    assert finding.severity == "warning"


def _assisted_manifest() -> dict[str, object]:
    """実 parser と同じ Assisted Manifest を database なしで生成する。

    導入期の決定的 draft 自体は蓝图を持たない。ただし発行は PREVIEW_READY な interpretation
    だけを入口とし、model の生成 Schema は蓝图を必須にしている。したがって gate が実際に見る
    assisted manifest は「決定的 draft + model の蓝图」であり、ここでもその形を再現する。
    """

    service = SkillService(None, ROOT / "contracts")  # type: ignore[arg-type]
    manifest = service.preview_inline(
        (InlineSkillFile(path="SKILL.md", content="# Gate Test\n"),)
    ).runtime_manifest_draft
    manifest["capability_blueprint"] = _capability_blueprint()
    return manifest


def test_assisted_manifest_requires_warning_acceptance() -> None:
    """Assisted compatibility は hard error ではなく管理者確認 warning とする。"""

    manifest = _assisted_manifest()
    interpretation_id = UUID(str(manifest["identity"]["interpretation_id"]))  # type: ignore[index]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(manifest["identity"]["source_hash"]),  # type: ignore[index]
        interpretation_id=interpretation_id,
    )

    assert passed is True
    assisted = next(item for item in findings if item.code == "assisted_review_required")
    assert assisted.severity == "warning"


def test_unregistered_tool_and_script_are_hard_failures() -> None:
    """Schema 内の Tool/script 宣言でも registry と checksum がなければ拒否する。"""

    manifest = deepcopy(_assisted_manifest())
    identity = manifest["identity"]  # type: ignore[index]
    compatibility = manifest["compatibility"]  # type: ignore[index]
    compatibility["level"] = "adapted"  # type: ignore[index]
    manifest["tools"] = [{"capability": "unknown.read/v1", "required": True}]
    manifest["workflows"][0]["steps"] = [{"key": "run", "kind": "script"}]  # type: ignore[index]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),  # type: ignore[index]
        interpretation_id=UUID(str(identity["interpretation_id"])),  # type: ignore[index]
    )

    assert passed is False
    assert {"tool_capability_unregistered", "script_checksum_missing"}.issubset(
        {item.code for item in findings}
    )


def test_document_read_tool_is_registered_capability() -> None:
    """document.read/v1 を Tool に宣言しても未登録 capability 判定を受けない。"""

    manifest = deepcopy(_assisted_manifest())
    identity = manifest["identity"]  # type: ignore[index]
    manifest["compatibility"]["level"] = "adapted"  # type: ignore[index]
    manifest["tools"] = [{"capability": "document.read/v1", "required": True}]

    _passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash=str(identity["source_hash"]),  # type: ignore[index]
        interpretation_id=UUID(str(identity["interpretation_id"])),  # type: ignore[index]
    )

    # 登録済みなので未登録 capability の hard gate は立たない (他の gate は問わない)。
    assert "tool_capability_unregistered" not in {item.code for item in findings}


def _task_contract() -> dict[str, object]:
    """Nested object/array/scalar を含む汎用 contract draft を返す。"""

    return {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "object",
        "description": "Review request",
        "fields": [
            {
                "key": "target_path",
                "type": "string",
                "required": True,
                "description": "Review target",
                "min_length": 1,
                "max_length": 1024,
                "pattern": "^[a-zA-Z0-9_./-]+$",
            },
            {
                "key": "options",
                "type": "object",
                "required": False,
                "fields": [
                    {
                        "key": "severity",
                        "type": "string",
                        "required": True,
                        "enum": ["low", "medium", "high"],
                    },
                    {
                        "key": "tags",
                        "type": "array",
                        "required": False,
                        "items": {"type": "string", "max_length": 64},
                    },
                ],
            },
        ],
    }


def _generated_manifest() -> dict[str, object]:
    """Compiler 成果物を inline で固定した generated contract Manifest を返す。"""

    input_contract = _task_contract()
    output_contract = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "object",
        "fields": [
            {
                "key": "summary",
                "type": "string",
                "required": True,
                "description": "Review summary",
            }
        ],
    }
    compiled_input = compile_task_contract(input_contract)
    compiled_output = compile_task_contract(output_contract)
    return {
        "manifest_version": "projectmind/v1alpha1",
        "identity": {
            "skill_key": "repository-review",
            "source_hash": "sha256:" + ("a" * 64),
            "interpretation_id": "00000000-0000-4000-8000-000000000123",
            "interpreter_version": "projectmind-skill-interpreter/2.1.0",
        },
        "compatibility": {"level": "adapted"},
        "tasks": [
            {
                "key": "review-file",
                "input_contract": input_contract,
                "output_contract": output_contract,
                "input_schema": compiled_input.schema,
                "output_schema": compiled_output.schema,
                "input_schema_checksum": compiled_input.checksum,
                "output_schema_checksum": compiled_output.checksum,
                "contract_source_trace": [
                    {
                        "contract": "input",
                        "field_path": "/target_path",
                        "source_path": "SKILL.md",
                        "source_section": "Inputs",
                        "line": 8,
                    },
                    {
                        "contract": "output",
                        "field_path": "/summary",
                        "source_path": "SKILL.md",
                        "source_section": "Output",
                        "line": 15,
                    },
                ],
            }
        ],
        # 蓝图は発行の必須要素になったため、基準 fixture が最初から備える。
        "capability_blueprint": _capability_blueprint(),
    }


def _capability_blueprint() -> dict[str, object]:
    """発行 gate を通過する最小の能力蓝图を返す。"""

    return {
        "blueprint_version": "projectmind.capability-blueprint/v1",
        "identity": {
            "skill_key": "repository-review",
            "source_hash": "sha256:" + ("a" * 64),
            "interpretation_id": "00000000-0000-4000-8000-000000000123",
            "interpreter_version": "projectmind-skill-interpreter/2.3.0",
        },
        "compatibility": {"level": "adapted"},
        "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
        "tasks": [
            {
                "key": "review-file",
                "capability": "repository.review",
                "objective": "Review the selected file and report findings.",
            }
        ],
        "resource_requirements": [],
        "guidance": {
            "required_rules": [],
            "recommended_steps": [],
            "quality_criteria": [],
            "prohibited_actions": [],
        },
        "source_traces": [
            {
                "target": "/tasks/0",
                "path": "SKILL.md",
                "line": 5,
                "reason": "The Goal section defines the review objective.",
            }
        ],
    }


def test_task_contract_compiler_is_deterministic_and_closes_objects() -> None:
    """Key 順差で checksum が変わらず、object は追加 property を拒否する。"""

    draft = _task_contract()
    reordered = {key: draft[key] for key in reversed(draft)}

    first = compile_task_contract(draft)
    second = compile_task_contract(reordered)

    assert first == second
    assert first.schema["additionalProperties"] is False
    assert first.schema["required"] == ["target_path"]
    assert first.schema["properties"]["options"]["required"] == ["severity"]
    Draft202012Validator.check_schema(first.schema)


def test_task_contract_compiler_rejects_unknown_key_and_unsafe_pattern() -> None:
    """外部 `$ref` と高リスク regex construct を元モデル境界で拒否する。"""

    external = _task_contract()
    external["$ref"] = "https://example.com/schema"
    unsafe_pattern = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "string",
        "pattern": "(a+)+$",
    }

    with pytest.raises(TaskContractCompilationError) as external_error:
        compile_task_contract(external)
    with pytest.raises(TaskContractCompilationError) as pattern_error:
        compile_task_contract(unsafe_pattern)

    assert external_error.value.code == "contract_keyword_unknown"
    assert pattern_error.value.code == "contract_pattern_unsafe"


def test_task_contract_compiler_enforces_size_and_depth_limits() -> None:
    """Field、enum、description、再帰 depth の各上限を contract 全体へ適用する。"""

    too_many = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "object",
        "fields": [
            {"key": f"field_{index}", "type": "string", "required": False}
            for index in range(MAX_CONTRACT_FIELDS + 1)
        ],
    }
    too_many_enum = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "integer",
        "enum": list(range(MAX_ENUM_VALUES + 1)),
    }
    too_long = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "string",
        "description": "x" * (MAX_DESCRIPTION_LENGTH + 1),
    }
    nested: dict[str, object] = {"type": "string"}
    for _ in range(6):
        nested = {"type": "array", "items": nested}
    too_deep = {"contract_version": TASK_CONTRACT_VERSION, **nested}

    cases = (
        (too_many, "contract_field_limit_exceeded"),
        (too_many_enum, "contract_enum_limit_exceeded"),
        (too_long, "contract_description_too_long"),
        (too_deep, "contract_depth_exceeded"),
    )
    for draft, code in cases:
        with pytest.raises(TaskContractCompilationError) as error:
            compile_task_contract(draft)
        assert error.value.code == code


def test_task_contract_compiler_rejects_duplicate_and_inapplicable_keywords() -> None:
    """曖昧な同名 field と type に無効な constraint を拒否する。"""

    duplicate = _task_contract()
    duplicate["fields"] = [
        {"key": "same", "type": "string", "required": True},
        {"key": "same", "type": "integer", "required": False},
    ]
    invalid_keyword = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "boolean",
        "minimum": 1,
    }

    with pytest.raises(TaskContractCompilationError) as duplicate_error:
        compile_task_contract(duplicate)
    with pytest.raises(TaskContractCompilationError) as keyword_error:
        compile_task_contract(invalid_keyword)

    assert duplicate_error.value.code == "contract_field_duplicate"
    assert keyword_error.value.code == "contract_keyword_inapplicable"


def test_runtime_manifest_schema_accepts_generated_and_rejects_legacy_mode() -> None:
    """Release D では generated inline だけを受理し、legacy `$ref` を拒否する。"""

    schema = ManifestValidator(ROOT / "contracts")._manifest_schema
    generated = _generated_manifest()
    legacy = deepcopy(generated)
    legacy["tasks"] = [
        {
            "key": "review-file",
            "input_schema": {"$ref": "legacy/input.schema.json"},
            "output_schema": {"$ref": "legacy/output.schema.json"},
        }
    ]

    Draft202012Validator(schema).validate(generated)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(legacy)


def test_generated_contract_gate_does_not_read_business_schema_file() -> None:
    """Source Schema なしで再コンパイル結果と checksum だけを検証して publish gate を通す。"""

    manifest = _generated_manifest()

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is True, findings
    assert not [finding for finding in findings if finding.severity == "error"]


def test_generated_contract_gate_rejects_schema_and_checksum_tampering() -> None:
    """Compile 成果物の改変を調整可能な安定 finding code で拒否する。"""

    manifest = _generated_manifest()
    task = manifest["tasks"][0]  # type: ignore[index]
    task["input_schema"]["additionalProperties"] = True  # type: ignore[index]
    task["output_schema_checksum"] = "sha256:" + ("0" * 64)  # type: ignore[index]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is False
    assert {
        "task_contract_schema_mismatch",
        "task_contract_checksum_mismatch",
    }.issubset({finding.code for finding in findings})


def test_declared_capability_blueprint_passes_the_publish_gate() -> None:
    """契約に適合する蓝图は publish gate を通る。"""

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        _generated_manifest(),
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is True
    assert not [item for item in findings if item.code.startswith("capability_blueprint")]


def test_run_scoped_workspace_tool_does_not_invent_a_resource_requirement() -> None:
    """隔離 workspace Tool は新 Integration を読まないため、虚偽の資源候補を要求しない。"""

    manifest = _generated_manifest()
    manifest["tools"] = [
        {"capability": "workspace.read/v1", "required": True},
        {"capability": "workspace.write/v1", "required": True},
    ]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is True
    assert "capability_blueprint:resource_not_disclosed" not in {
        item.code for item in findings
    }


def test_change_propose_is_platform_scoped_but_apply_provider_is_not_agent_tool() -> None:
    """Proposal control Tool は許可し、実 apply capability の Agent 露出を拒否する。"""

    manifest = _generated_manifest()
    manifest["tools"] = [{"capability": "change.propose/v1", "required": False}]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )
    assert passed is True, findings

    # 登録済み write capability はいずれも Agent Tool として宣言できない。片方だけ守ると、
    # 新しい apply 能力を足したときに「Agent が直接呼べる書き込み」が静かに生まれる。
    for capability in ("issue.update/v1", "repository.write/v1"):
        manifest["tools"] = [{"capability": capability, "required": True}]
        passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
            manifest,
            source_hash="sha256:" + ("a" * 64),
            interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
        )
        assert passed is False, capability
        assert "effect_capability_exposed_to_agent" in {
            item.code for item in findings
        }, capability


def test_missing_capability_blueprint_is_a_gate_error() -> None:
    """蓝图を持たない Manifest は発行できない。

    蓝图は利用者が審査する主産物であり、Run へ渡す guidance の正本でもある。欠けたまま発行
    できると、目標も必須規則も持たない version が実行経路へ入る。
    """

    manifest = _generated_manifest()
    del manifest["capability_blueprint"]
    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is False
    assert "capability_blueprint_missing" in {item.code for item in findings}


def test_blueprint_apply_intent_without_write_resource_fails_the_gate() -> None:
    """緩い manifest envelope を通り抜けた apply 意図を gate が捕まえる。

    effect intent は権限ではない。資源束縛の無い apply が発行されると、Project 側で scope を
    固定できないまま「宣言済みの効果」として凍結される。
    """

    manifest = _generated_manifest()
    blueprint = manifest["capability_blueprint"]
    blueprint["resource_requirements"] = [  # type: ignore[index]
        {"key": "tracker", "kind": "issue", "required": False, "access": "read"}
    ]
    blueprint["effect_intents"] = [  # type: ignore[index]
        {
            "key": "update-tracker",
            "mode": "apply",
            "resource_key": "tracker",
            "operation": "Record the outcome on the tracked issue.",
            "risk": "medium",
            "approval_mode": "ask",
        }
    ]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is False
    assert "capability_blueprint:effect_resource_not_writable" in {
        finding.code for finding in findings
    }


def test_blueprint_required_rule_without_trace_fails_the_gate() -> None:
    """根拠 trace の無い必需規則を発行前に止める。"""

    manifest = _generated_manifest()
    blueprint = manifest["capability_blueprint"]
    blueprint["guidance"]["required_rules"] = [  # type: ignore[index]
        {"key": "invented", "text": "Always escalate to the release manager."}
    ]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is False
    assert "capability_blueprint:required_rule_source_trace_missing" in {
        finding.code for finding in findings
    }


def test_blueprint_must_disclose_every_capability_the_manifest_reads() -> None:
    """Manifest が読む資源を蓝图が落としたまま発行できない。

    利用者が Preview で見るのは蓝图である。蓝图が資源を伏せると、開示されていない資源を読む
    Run が発行でき、就緒度も実態より軽く出る。
    """

    manifest = _generated_manifest()
    manifest["tools"] = [{"capability": "repository.read/v1", "required": True}]
    # 蓝图側は資源を一切宣言しない。
    manifest["capability_blueprint"]["resource_requirements"] = []  # type: ignore[index]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is False
    assert "capability_blueprint:resource_not_disclosed" in {item.code for item in findings}


def test_disclosed_capability_passes_the_gate() -> None:
    """蓝图が同じ capability を開示していれば Tool 宣言は許可される。"""

    manifest = _generated_manifest()
    manifest["tools"] = [{"capability": "repository.read/v1", "required": True}]
    manifest["capability_blueprint"]["resource_requirements"] = [  # type: ignore[index]
        {
            "key": "repository-source",
            "kind": "repository",
            "required": True,
            "access": "read",
            "capabilities": ["repository.read/v1"],
        }
    ]

    passed, findings = ManifestValidator(ROOT / "contracts").evaluate(
        manifest,
        source_hash="sha256:" + ("a" * 64),
        interpretation_id=UUID("00000000-0000-4000-8000-000000000123"),
    )

    assert passed is True
    assert not [item for item in findings if item.code.startswith("capability_blueprint")]
