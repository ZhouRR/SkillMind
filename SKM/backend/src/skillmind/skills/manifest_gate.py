"""RuntimeManifest の publish 可否を決定的 hard gate で判定する。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from skillmind.core.hashing import canonical_json
from skillmind.skills.design_validation import (
    SkillDesignInvalidError,
    SkillDesignSource,
    validate_skill_design,
)
from skillmind.skills.document_prerequisites import document_prerequisites
from skillmind.skills.domain import InlineSkillFile, ManifestGateFinding
from skillmind.skills.task_contract import (
    TaskContractCompilationError,
    compile_task_contract,
)

_OPTIONAL_INTERPRETATION_DIAGNOSTICS = frozenset(
    {
        "missing:view_spec",
        "missing:test_fixture",
        "view_binding_missing",
    }
)

# Platform 所有の Run-scoped Tool は既に束縛済み資源か実行制御だけを扱い、新しい
# Integration/resource を導入しない。Blueprint の resource requirement 開示対象とは分離する。
_RUN_SCOPED_PLATFORM_CAPABILITIES = frozenset(
    {
        "change.propose/v1",
        "interaction.request/v1",
        "subagent.dispatch/v1",
        "workspace.read/v1",
        "workspace.search/v1",
        "workspace.write/v1",
        "workspace.write/v2",
        "document.readiness/v1",
    }
)

# Apply Provider は Agent MCP registry へ出さず、Approval/EffectExecution Worker だけが呼ぶ。
# 登録済み write capability は必ずここへ加える (§20 で `repository.write/v1` を追加)。抜けると
# Skill が apply 能力を Agent Tool として宣言でき、publish gate がそれを黙って通してしまう。
_EFFECT_ONLY_CAPABILITIES = frozenset({"issue.update/v1", "repository.write/v1"})

# Guidance 本文が名指しする能力識別子 (`issue.read/v1` 形)。散文と区別するため、
# 少なくとも一つの `.` と `/v<数字>` を持つ token だけを重表達先の名指しとみなす。
# 先行の lookbehind は scheme や path 途中からの部分一致を禁じ、`https://x.example.com/v1`
# のような URL を除く。scheme を持たない裸の host 断片は能力識別子と構文上同形のため残るが、
# 接続情報は手順に現れてはならない以上、その報告は境界違反の指摘として妥当である。
_CAPABILITY_REFERENCE_PATTERN = re.compile(r"(?<![\w./-])[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+/v[0-9]+")

# Guidance の note list。順序は pointer をそのまま source trace の target と揃えるため保つ。
_GUIDANCE_NOTE_FIELDS = (
    "required_rules",
    "recommended_steps",
    "quality_criteria",
    "prohibited_actions",
)


class ManifestValidator:
    """Schema、binding、Tool、permission、script、ViewSpec を publish 前に検査する。"""

    def __init__(
        self,
        contracts_dir: Path,
        *,
        registered_capabilities: frozenset[str] | None = None,
    ) -> None:
        """Versioned contract root と Manifest/ViewSpec Schema を読み込む。"""

        self._contracts_dir = contracts_dir.resolve()
        self._manifest_schema = self._load_json("runtime-manifest/v1alpha1.schema.json")
        self._view_schema = self._load_json("view-spec/v1alpha1.schema.json")
        self._registered_capabilities = (
            registered_capabilities
            if registered_capabilities is not None
            else self._load_registered_capabilities()
        )
        Draft202012Validator.check_schema(self._manifest_schema)
        Draft202012Validator.check_schema(self._view_schema)

    @property
    def registered_capabilities(self) -> frozenset[str]:
        """Publish gate が認める登録済み Tool capability 集合を公開する。

        就緒度計算は同じ集合を正本にする必要がある。別々に読み込むと、発行はできるのに
        永遠に設定待ちのままになるような食い違いが生じる。
        """

        return self._registered_capabilities

    def evaluate(
        self,
        source: SkillDesignSource,
    ) -> tuple[bool, tuple[ManifestGateFinding, ...]]:
        """原 source と凍結 Manifest を検査し、変更せず発行判断を返す。

        不完全な DRAFT は保存できる finding に変換する。source を欠く構文検査だけでは
        発行可能にせず、既存版を normalizer で補修して通過させることもしない。
        """

        try:
            design = validate_skill_design(source=source, contracts_dir=self._contracts_dir)
        except SkillDesignInvalidError as error:
            return False, (self._error(error.code, str(error), None),)
        manifest = design.manifest
        blueprint = design.blueprint
        assert blueprint is not None
        findings: list[ManifestGateFinding] = []
        for task in _object_list(blueprint.get("tasks")):
            try:
                document_prerequisites(manifest, str(task.get("key", "")))
            except ValueError:
                findings.append(self._error(
                    "document_prerequisites_invalid",
                    "Document prerequisites require a supported readiness Tool and apply intents",
                    "/capability_blueprint/tasks",
                ))


        compatibility = _mapping(manifest, "compatibility")
        if compatibility.get("level") == "assisted":
            findings.append(
                ManifestGateFinding(
                    code="assisted_review_required",
                    severity="warning",
                    message="Assisted compatibility requires explicit administrator acceptance",
                    path="/compatibility/level",
                )
            )
        diagnostics = compatibility.get("diagnostics")
        if isinstance(diagnostics, list):
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, dict):
                    continue
                severity = diagnostic.get("severity")
                code = diagnostic.get("code")
                if severity in {"error", "warning"} and isinstance(code, str):
                    effective_severity = (
                        "warning" if code in _OPTIONAL_INTERPRETATION_DIAGNOSTICS else severity
                    )
                    findings.append(
                        ManifestGateFinding(
                            code=f"interpretation:{code}",
                            severity=effective_severity,
                            message=str(diagnostic.get("message", code)),
                        )
                    )

        self._check_bindings(manifest, findings)
        self._check_generated_contracts(manifest, findings)
        self._check_blueprint_discloses_manifest_resources(manifest, blueprint, findings)
        self._check_step_capability_references(blueprint, findings)
        self._check_optional_assets(
            manifest,
            findings,
            source_files=tuple(
                InlineSkillFile(path=item["path"], content=item["content"])
                for item in source.source_snapshot
            ),
        )
        passed = not any(finding.severity == "error" for finding in findings)
        return passed, tuple(findings)

    def _check_step_capability_references(
        self, blueprint: Mapping[str, Any], findings: list[ManifestGateFinding]
    ) -> None:
        """Guidance が名指しする能力が登録済みかつ資源要求で開示済みかを検査する。

        docs/11 §5.4 の重表達で Interpreter は `svn cat` のような手順を能力呼び出しへ書き換える
        (計画 §21)。書き換え先が実在しない、あるいはどの資源要求にも現れないなら、その手順は
        Run で必ず実行不能になる。guidance は自由文で Schema には掛からないため、名指しだけを
        ここで決定的に照合する。識別子を持たない降格 guidance (「この能力が載るまで実行でき
        ない」) は名指しが無いので素通りする。
        判定を warning に留めるのは D-A3 による。手順は資源宣言そのものではなく、一手順が落ちた
        だけで Skill 全体の発行を止めると「対齐できなければ失敗」へ逆戻りする。就緒度は資源要求
        から算出するものであり、ここでは動かさない。
        """

        disclosed = {
            hint
            for requirement in _object_list(blueprint.get("resource_requirements"))
            for hint in requirement.get("capabilities", [])
            if isinstance(hint, str)
        }
        guidance = _mapping(blueprint, "guidance")
        for field in _GUIDANCE_NOTE_FIELDS:
            for index, note in enumerate(_object_list(guidance.get(field))):
                text = note.get("text")
                if not isinstance(text, str):
                    continue
                path = f"/capability_blueprint/guidance/{field}/{index}"
                for capability in sorted(set(_CAPABILITY_REFERENCE_PATTERN.findall(text))):
                    if capability not in self._registered_capabilities:
                        findings.append(
                            self._warning(
                                "capability_blueprint:step_capability_unregistered",
                                (
                                    "Guidance names a capability that is not registered: "
                                    f"{capability}"
                                ),
                                path,
                            )
                        )
                    elif (
                        capability not in disclosed
                        and capability not in _RUN_SCOPED_PLATFORM_CAPABILITIES
                    ):
                        findings.append(
                            self._warning(
                                "capability_blueprint:step_capability_not_disclosed",
                                (
                                    "Guidance names a capability that no resource requirement "
                                    f"discloses: {capability}"
                                ),
                                path,
                            )
                        )

    def _check_blueprint_discloses_manifest_resources(
        self,
        manifest: Mapping[str, Any],
        blueprint: Mapping[str, Any],
        findings: list[ManifestGateFinding],
    ) -> None:
        """Manifest の Tool がすべて蓝图の資源要求に開示されていることを検査する。

        資源要求は蓝图が唯一の宣言元であり、Run が束縛する資源はそこから解決する。残る食い違い
        は「Tool は宣言されているのに、その capability を要する資源要求が無い」場合であり、
        利用者が Preview で見る蓝图に現れない資源を Run が触ることになるため hard deny する。
        逆向き(要求にあって Tool 未登録)は readiness を下げる話で、ここでは扱わない。
        """

        disclosed = {
            hint
            for requirement in _object_list(blueprint.get("resource_requirements"))
            for hint in requirement.get("capabilities", [])
            if isinstance(hint, str)
        }
        declared: dict[str, str] = {}
        for tool in _object_list(manifest.get("tools")):
            if isinstance((capability := tool.get("capability")), str):
                if capability in _RUN_SCOPED_PLATFORM_CAPABILITIES:
                    continue
                declared.setdefault(capability, "/tools")
        for capability, path in sorted(declared.items()):
            if capability not in disclosed:
                findings.append(
                    self._error(
                        "capability_blueprint:resource_not_disclosed",
                        (
                            "RuntimeManifest reads a capability that the CapabilityBlueprint "
                            f"does not disclose: {capability}"
                        ),
                        path,
                    )
                )

    def _check_bindings(
        self, manifest: Mapping[str, Any], findings: list[ManifestGateFinding]
    ) -> None:
        """Task 参照と登録済み capability、script checksum gate を検査する。"""

        capabilities = {item.get("key") for item in _object_list(manifest.get("capabilities"))}
        workflows = {item.get("key") for item in _object_list(manifest.get("workflows"))}
        for task in _object_list(manifest.get("tasks")):
            if task.get("capability") not in capabilities:
                findings.append(
                    self._error(
                        "task_capability_missing", "Task capability is not declared", "/tasks"
                    )
                )
            if task.get("workflow") not in workflows:
                findings.append(
                    self._error("task_workflow_missing", "Task workflow is not declared", "/tasks")
                )
        for item in _object_list(manifest.get("tools")):
            capability = item.get("capability")
            if isinstance(capability, str) and capability not in self._registered_capabilities:
                findings.append(
                    self._error(
                        "tool_capability_unregistered",
                        f"Capability is not registered: {capability}",
                        "/tools",
                    )
                )
            if isinstance(capability, str) and capability in _EFFECT_ONLY_CAPABILITIES:
                findings.append(
                    self._error(
                        "effect_capability_exposed_to_agent",
                        (
                            "Apply capability must be declared through a Blueprint effect intent, "
                            "not exposed as a direct Agent Tool"
                        ),
                        "/tools",
                    )
                )
        if any(
            step.get("kind") == "script"
            for workflow in _object_list(manifest.get("workflows"))
            for step in _object_list(workflow.get("steps"))
        ):
            findings.append(
                self._error(
                    "script_checksum_missing",
                    "Script steps require a published checksum registry",
                    "/workflows",
                )
            )

    def _check_optional_assets(
        self,
        manifest: Mapping[str, Any],
        findings: list[ManifestGateFinding],
        *,
        source_files: Sequence[InlineSkillFile],
    ) -> None:
        """任意 ViewSpec と test fixture だけを source snapshot から安全に検査する。"""

        refs: list[tuple[str, str]] = []
        for view in _object_list(_mapping(manifest, "ui").get("views")):
            reference = view.get("$ref")
            if isinstance(reference, str):
                refs.append((reference, "view"))
        for test in _object_list(manifest.get("tests")):
            fixture = test.get("fixture")
            if isinstance(fixture, str):
                refs.append((fixture, "fixture"))
        resolved_view_keys: set[str] = set()
        source_contracts = {item.path: item.content for item in source_files}
        for reference, kind in refs:
            try:
                value = self._load_contract_reference(reference, source_contracts)
            except (OSError, ValueError) as error:
                findings.append(self._warning("optional_asset_invalid", str(error), reference))
                continue
            if value is None:
                findings.append(
                    self._warning(
                        "optional_asset_missing",
                        f"Optional contract reference does not exist: {reference}",
                        reference,
                    )
                )
                continue
            if kind == "view":
                try:
                    Draft202012Validator(
                        self._view_schema, format_checker=FormatChecker()
                    ).validate(value)
                    view_key = value.get("key")
                    if isinstance(view_key, str):
                        resolved_view_keys.add(view_key)
                except (ValidationError, ValueError) as error:
                    findings.append(self._warning("optional_asset_invalid", str(error), reference))
        required_view_keys = {
            task.get("view")
            for task in _object_list(manifest.get("tasks"))
            if isinstance(task.get("view"), str)
        }
        default_view = _mapping(manifest, "ui").get("default_view")
        if isinstance(default_view, str):
            required_view_keys.add(default_view)
        required_view_keys.discard("standard")
        if not required_view_keys.issubset(resolved_view_keys):
            findings.append(
                self._warning(
                    "view_fallback_required",
                    "Custom view is unavailable; standard schema view will be used",
                    "/ui/views",
                )
            )

    def _check_generated_contracts(
        self,
        manifest: Mapping[str, Any],
        findings: list[ManifestGateFinding],
    ) -> None:
        """Generated contract を再コンパイルし、Schema と checksum の一致を検査する。"""

        for task_index, task in enumerate(_object_list(manifest.get("tasks"))):
            for contract_name in ("input", "output"):
                contract_key = f"{contract_name}_contract"
                raw_contract = task.get(contract_key)
                if raw_contract is None:
                    continue
                contract_path = f"/tasks/{task_index}/{contract_key}"
                if not isinstance(raw_contract, Mapping):
                    findings.append(
                        self._error(
                            "task_contract_invalid",
                            "Task contract must be an object",
                            contract_path,
                        )
                    )
                    continue
                try:
                    compiled = compile_task_contract(raw_contract)
                except TaskContractCompilationError as error:
                    findings.append(
                        self._error(
                            "task_contract_invalid",
                            f"{error.code}: {error.message}",
                            f"{contract_path}{error.path}",
                        )
                    )
                    continue
                schema_key = f"{contract_name}_schema"
                checksum_key = f"{contract_name}_schema_checksum"
                raw_schema = task.get(schema_key)
                raw_checksum = task.get(checksum_key)
                if not isinstance(raw_schema, Mapping) or canonical_json(
                    dict(raw_schema)
                ) != canonical_json(compiled.schema):
                    findings.append(
                        self._error(
                            "task_contract_schema_mismatch",
                            "Compiled Schema does not match TaskContractDraft",
                            f"/tasks/{task_index}/{schema_key}",
                        )
                    )
                if raw_checksum != compiled.checksum:
                    findings.append(
                        self._error(
                            "task_contract_checksum_mismatch",
                            "Schema checksum does not match TaskContractDraft",
                            f"/tasks/{task_index}/{checksum_key}",
                        )
                    )

    def _load_contract_reference(
        self, reference: str, source_contracts: Mapping[str, str]
    ) -> dict[str, Any] | None:
        """Source snapshot または platform root から JSON object contract を読む。"""

        path = self._safe_contract_path(reference)
        if path is None:
            raise ValueError("Contract reference must be a safe relative path")
        normalized_reference = PurePosixPath(reference).as_posix()
        if normalized_reference in source_contracts:
            value = json.loads(source_contracts[normalized_reference])
        elif path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            return None
        if not isinstance(value, dict):
            raise ValueError("Contract must be a JSON object")
        return cast(dict[str, Any], value)

    def _safe_contract_path(self, reference: str) -> Path | None:
        """External URI、fragment、root 逸脱を拒否して contract path を返す。"""

        if "://" in reference or "#" in reference:
            return None
        relative = PurePosixPath(reference)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        candidate = (self._contracts_dir / relative.as_posix()).resolve()
        return candidate if candidate.is_relative_to(self._contracts_dir) else None

    def _load_json(self, relative: str) -> dict[str, Any]:
        """Contract JSON object を UTF-8 で読み込む。"""

        value = json.loads((self._contracts_dir / relative).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Contract must be an object: {relative}")
        return cast(dict[str, Any], value)

    def _load_registered_capabilities(self) -> frozenset[str]:
        """Frozen Tool catalog から publish gate の登録 capability 集合を構築する。"""

        catalog = self._load_json("examples/skill-capability-catalog.v1.json")
        capabilities = catalog.get("capabilities")
        if not isinstance(capabilities, list):
            raise ValueError("Tool capability catalog must contain an array")
        return frozenset(
            capability
            for item in capabilities
            if isinstance(item, dict) and isinstance((capability := item.get("capability")), str)
        )

    @staticmethod
    def _error(code: str, message: str, path: str | None) -> ManifestGateFinding:
        """Hard gate error finding を生成する。"""

        return ManifestGateFinding(code=code, severity="error", message=message, path=path)

    @staticmethod
    def _warning(code: str, message: str, path: str | None) -> ManifestGateFinding:
        """管理者 acceptance で解決可能な非安全 finding を生成する。"""

        return ManifestGateFinding(code=code, severity="warning", message=message, path=path)


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Manifest の object field を空 object fallback 付きで返す。"""

    item = value.get(key)
    return cast(dict[str, Any], item) if isinstance(item, dict) else {}


def _object_list(value: Any) -> list[dict[str, Any]]:
    """Manifest array から object item だけを返す。"""

    return (
        [cast(dict[str, Any], item) for item in value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value)
        else []
    )
