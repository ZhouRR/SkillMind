"""Skill Interpreter 前段の静的分析、catalog snapshot、offline 契約検証を提供する。"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.redaction import contains_sensitive_content, find_sensitive_key
from skillmind.skills.capability_blueprint import (
    CapabilityBlueprintValidator,
    bind_blueprint_identity,
)
from skillmind.skills.domain import InlineSkillFile
from skillmind.skills.importer import NormalizedSkillPackage, SkillPackageParser
from skillmind.skills.runtime_defaults import normalize_runtime_manifest
from skillmind.skills.task_contract import MAX_CONTRACT_DEPTH, compile_task_contract

_VERSIONED_CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_URL = re.compile(r"(?P<scheme>https?)://[^\s<>()\]]+", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```\s*(?P<language>[A-Za-z0-9_-]*)\s*$")
_SHELL_LANGUAGES = frozenset({"bash", "cmd", "console", "powershell", "ps1", "sh", "shell", "zsh"})
# 内蔵 Tool の「宣言」は untrusted evidence であって授与ではない。該当しても解釈は止めず、
# 能力への重表達へ回す非ブロッキング信号に留める。実際の授与は permission snapshot と
# tool registry が拒否し続ける。
_BUILTIN_TOOL_DECLARATIONS = frozenset({"bash", "edit", "web", "write"})
_SENSITIVE_FILE_NAMES = frozenset({".env", "id_dsa", "id_ed25519", "id_rsa"})
_SCRIPT_LANGUAGES = {
    ".bash": "shell",
    ".cmd": "cmd",
    ".js": "javascript",
    ".ps1": "powershell",
    ".py": "python",
    ".sh": "shell",
    ".ts": "typescript",
}


class UnsafeSkillSourceError(ValueError):
    """Model 呼び出し前に拒否すべき source analysis を表す。"""


@dataclass(frozen=True, slots=True)
class CapabilityCatalogEntry:
    """Interpreter に公開してよい一つの versioned Tool capability。"""

    capability: str
    description: str
    request_schema: str
    response_schema: str
    error_schema: str
    providers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Secret や Integration identity を含まない JSON entry を返す。"""

        return {
            "capability": self.capability,
            "description": self.description,
            "request_schema": self.request_schema,
            "response_schema": self.response_schema,
            "error_schema": self.error_schema,
            "providers": list(self.providers),
        }


@dataclass(frozen=True, slots=True)
class CapabilityCatalogSnapshot:
    """Interpreter request に固定する capability catalog と checksum。"""

    catalog_version: str
    capabilities: tuple[CapabilityCatalogEntry, ...]
    checksum: str

    @classmethod
    def build(
        cls,
        *,
        catalog_version: str,
        capabilities: Sequence[CapabilityCatalogEntry],
    ) -> CapabilityCatalogSnapshot:
        """Entry を検証・安定順化し、共通 canonical JSON から checksum を作る。"""

        if not catalog_version:
            raise ValueError("Capability catalog version must not be empty")
        ordered = tuple(sorted(capabilities, key=lambda item: item.capability))
        if not ordered:
            raise ValueError("Capability catalog must contain at least one entry")
        seen: set[str] = set()
        for item in ordered:
            if not _VERSIONED_CAPABILITY.fullmatch(item.capability):
                raise ValueError(f"Invalid versioned capability: {item.capability}")
            if item.capability in seen:
                raise ValueError(f"Duplicate capability: {item.capability}")
            if not item.description or not item.providers:
                raise ValueError(f"Incomplete capability catalog entry: {item.capability}")
            if any(not provider for provider in item.providers) or len(set(item.providers)) != len(
                item.providers
            ):
                raise ValueError(f"Capability Provider must not be empty: {item.capability}")
            seen.add(item.capability)
        body = {
            "catalog_version": catalog_version,
            "capabilities": [item.to_dict() for item in ordered],
        }
        return cls(
            catalog_version=catalog_version,
            capabilities=ordered,
            checksum=f"sha256:{sha256_hex(canonical_json(body))}",
        )

    def to_dict(self) -> dict[str, Any]:
        """Version、entry、checksum を含む frozen snapshot を返す。"""

        return {
            "catalog_version": self.catalog_version,
            "checksum": self.checksum,
            "capabilities": [item.to_dict() for item in self.capabilities],
        }


@dataclass(frozen=True, slots=True)
class InterpreterSystemSkillIdentity:
    """Versioned system Skill と prompt source checksum の identity。"""

    skill_key: str
    version: str
    source_hash: str
    prompt_checksum: str

    @property
    def interpreter_version(self) -> str:
        """RuntimeManifest に固定する interpreter version string を返す。"""

        return f"{self.skill_key}/{self.version}"

    def to_dict(self) -> dict[str, str]:
        """Interpreter request の identity object を返す。"""

        return {
            "skill_key": self.skill_key,
            "version": self.version,
            "source_hash": self.source_hash,
            "prompt_checksum": self.prompt_checksum,
        }


@dataclass(frozen=True, slots=True)
class StaticAnalysisDiagnostic:
    """Source 内容を複製せず問題の分類と位置だけを保持する。"""

    severity: str
    code: str
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Versioned diagnostic contract に一致する object を返す。"""

        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class SkillStaticAnalysis:
    """Model へ渡す前の deterministic source risk summary。"""

    source_hash: str
    blocked: bool
    references: tuple[str, ...]
    scripts: tuple[dict[str, Any], ...]
    commands: tuple[dict[str, Any], ...]
    external_links: tuple[dict[str, Any], ...]
    declared_tools: tuple[str, ...]
    unsupported_operations: tuple[dict[str, Any], ...]
    diagnostics: tuple[StaticAnalysisDiagnostic, ...]
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        """Raw command、URL、credential 値を含まない contract object を返す。"""

        return {
            "analysis_version": "skillmind.skill-static-analysis/v1",
            "source_hash": self.source_hash,
            "blocked": self.blocked,
            "references": list(self.references),
            "scripts": [dict(item) for item in self.scripts],
            "commands": [dict(item) for item in self.commands],
            "external_links": [dict(item) for item in self.external_links],
            "declared_tools": list(self.declared_tools),
            "unsupported_operations": [dict(item) for item in self.unsupported_operations],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "checksum": self.checksum,
        }


class SkillStaticAnalyzer:
    """Skill source を実行せず、Interpreter 前の hard block と risk を抽出する。"""

    def analyze(
        self,
        package: NormalizedSkillPackage,
        source_files: Sequence[InlineSkillFile],
    ) -> SkillStaticAnalysis:
        """Indexed text だけを走査し、値を複製しない安定順 report を生成する。"""

        texts = _validated_source_texts(package, source_files)
        diagnostics: list[StaticAnalysisDiagnostic] = []
        commands: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []

        sensitive_extension = find_sensitive_key(package.extensions)
        if sensitive_extension is not None:
            diagnostics.append(
                StaticAnalysisDiagnostic(
                    severity="error",
                    code="credential_metadata_not_allowed",
                    message="Credential-like metadata must be removed before interpretation",
                    path="SKILL.md",
                )
            )

        for indexed in package.files:
            name = PurePosixPath(indexed.path).name.lower()
            if name in _SENSITIVE_FILE_NAMES or name.endswith((".key", ".p12", ".pfx")):
                diagnostics.append(
                    StaticAnalysisDiagnostic(
                        severity="error",
                        code="credential_file_not_allowed",
                        message="Credential-like files must not enter interpretation",
                        path=indexed.path,
                    )
                )

        for path, text in sorted(texts.items()):
            shell_language: str | None = None
            for line_number, line in enumerate(text.splitlines(), start=1):
                fence = _FENCE.match(line)
                if fence:
                    language = fence.group("language").lower()
                    if shell_language is None:
                        shell_language = language if language in _SHELL_LANGUAGES else ""
                    else:
                        shell_language = None
                    continue
                # 検出語彙は core/redaction の単一実装に集約し、storage 側の走査と漂流させない。
                if contains_sensitive_content(line):
                    diagnostics.append(
                        StaticAnalysisDiagnostic(
                            severity="error",
                            code="credential_literal_not_allowed",
                            message=(
                                "Credential-like source content must be removed before "
                                "interpretation"
                            ),
                            path=path,
                            line=line_number,
                        )
                    )
                for match in _URL.finditer(line):
                    links.append(
                        {"path": path, "line": line_number, "scheme": match.group("scheme").lower()}
                    )
                if shell_language and line.strip():
                    commands.append(
                        {"path": path, "line": line_number, "language": shell_language}
                    )
                    unsupported.append(
                        {
                            "code": "arbitrary_shell_not_supported",
                            "path": path,
                            "line": line_number,
                        }
                    )

        if commands:
            diagnostics.append(
                StaticAnalysisDiagnostic(
                    severity="warning",
                    code="shell_commands_require_manual_mapping",
                    message=(
                        "Shell commands are evidence only and cannot be executed by the interpreter"
                    ),
                )
            )
        if links:
            diagnostics.append(
                StaticAnalysisDiagnostic(
                    severity="info",
                    code="external_links_recorded",
                    message="External links were recorded without granting network access",
                )
            )
        if package.resources.scripts:
            diagnostics.append(
                StaticAnalysisDiagnostic(
                    severity="warning",
                    code="bundled_scripts_require_sandbox",
                    message="Bundled scripts require checksum registration and sandbox validation",
                )
            )
        for tool_name in package.declared_tools:
            # `Bash(python3 *)` のような引数付き宣言も基底名 `bash` へ畳んで同じ信号に載せる。
            # 裸形だけを精確一致で挟むと引数付きがすり抜けるため、基底名で判定する。
            if tool_name.split("(", 1)[0].strip().lower() in _BUILTIN_TOOL_DECLARATIONS:
                diagnostics.append(
                    StaticAnalysisDiagnostic(
                        severity="warning",
                        code="declared_builtin_tool",
                        message=(
                            "A declared built-in Tool is recorded for capability "
                            "re-expression; the interpreter never grants it"
                        ),
                        path="SKILL.md",
                    )
                )
                unsupported.append(
                    {"code": "declared_builtin_tool", "path": "SKILL.md", "line": None}
                )

        for imported in package.diagnostics:
            diagnostics.append(
                StaticAnalysisDiagnostic(
                    severity=imported.severity,
                    code=f"import:{imported.code}",
                    message=imported.message,
                    path=imported.path,
                )
            )

        diagnostics.sort(key=_diagnostic_sort_key)
        scripts = tuple(
            {
                "path": path,
                "language": _SCRIPT_LANGUAGES.get(PurePosixPath(path).suffix.lower(), "unknown"),
            }
            for path in sorted(package.resources.scripts)
        )
        body = {
            "analysis_version": "skillmind.skill-static-analysis/v1",
            "source_hash": package.content_hash,
            "blocked": any(item.severity == "error" for item in diagnostics),
            "references": sorted(package.resources.references),
            "scripts": list(scripts),
            "commands": sorted(commands, key=_located_sort_key),
            "external_links": sorted(links, key=_located_sort_key),
            "declared_tools": sorted(package.declared_tools),
            "unsupported_operations": sorted(unsupported, key=_unsupported_sort_key),
            "diagnostics": [item.to_dict() for item in diagnostics],
        }
        return SkillStaticAnalysis(
            source_hash=package.content_hash,
            blocked=cast(bool, body["blocked"]),
            references=tuple(cast(list[str], body["references"])),
            scripts=scripts,
            commands=tuple(cast(list[dict[str, Any]], body["commands"])),
            external_links=tuple(cast(list[dict[str, Any]], body["external_links"])),
            declared_tools=tuple(cast(list[str], body["declared_tools"])),
            unsupported_operations=tuple(
                cast(list[dict[str, Any]], body["unsupported_operations"])
            ),
            diagnostics=tuple(diagnostics),
            checksum=f"sha256:{sha256_hex(canonical_json(body))}",
        )


def load_interpreter_system_skill(
    root: Path,
    *,
    generation_schema: Mapping[str, Any] | None = None,
) -> InterpreterSystemSkillIdentity:
    """Versioned system Skill と任意 generation Schema から prompt identity を返す。"""

    package = SkillPackageParser().parse_directory(root.resolve(strict=True))
    version = package.extensions.get("version")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise ValueError("Interpreter system Skill must declare a semantic version")
    if package.name != "skillmind-skill-interpreter":
        raise ValueError("Unexpected interpreter system Skill name")
    prompt_checksum = package.content_hash
    if generation_schema is not None:
        # 実 prompt は system Skill package と generation Schema の双方で変わるため、source hash
        # だけでは旧 Schema の execution を誤って再利用する。両方を canonical identity に含める。
        prompt_identity = {
            "system_skill_source_hash": package.content_hash,
            "generation_schema": dict(generation_schema),
        }
        prompt_checksum = f"sha256:{sha256_hex(canonical_json(prompt_identity))}"
    return InterpreterSystemSkillIdentity(
        skill_key=package.name,
        version=version,
        source_hash=package.content_hash,
        prompt_checksum=prompt_checksum,
    )


def build_interpreter_generation_schema(contracts_dir: Path) -> dict[str, Any]:
    """Model に渡す response Schema へ Report と contract draft の形状を埋め込む。

    公開 response contract は各 artifact を独立検証できるよう分割している。一方、model 側へ
    頂層 envelope だけを渡すと nested object が無制約になり、SDK 検証後に platform 検証で
    落ちる。local ``$ref`` を解決した単一 Schema を生成して両者の制約差を閉じる。
    """

    root = contracts_dir.resolve(strict=True)
    response = _materialize_local_refs(
        _load_json(root / "skills/interpreter/v1/response.schema.json")
    )
    report = _materialize_local_refs(
        _load_json(root / "skills/interpreter/v1/interpretation-report.schema.json"),
        reference_root="#/properties/report",
    )
    manifest = _materialize_local_refs(
        _load_json(root / "runtime-manifest/v1alpha1.schema.json"),
        reference_root="#/properties/runtime_manifest_draft",
        bound_task_contracts=True,
    )
    blueprint = _materialize_local_refs(
        _load_json(root / "capability-blueprint/v1.schema.json"),
        reference_root="#/properties/runtime_manifest_draft/properties/capability_blueprint",
        bound_task_contracts=True,
    )
    _restrict_manifest_to_model_contract_drafts(manifest)
    _restrict_blueprint_to_model_fields(blueprint)
    _require_model_capability_blueprint(manifest, blueprint)
    properties = response.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Interpreter response Schema properties must be an object")
    properties["report"] = report
    properties["runtime_manifest_draft"] = manifest
    Draft202012Validator.check_schema(response)
    return response


def build_interpreter_request(
    *,
    package: NormalizedSkillPackage,
    analysis: SkillStaticAnalysis,
    catalog: CapabilityCatalogSnapshot,
    system_skill: InterpreterSystemSkillIdentity,
    previous_interpretation: Mapping[str, Any] | None = None,
    adjustment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Frozen deterministic input から model adapter 非依存 request を構築する。

    reinterpretation では previous_interpretation と adjustment を明示的に渡す。両者は
    request identity に含まれるため、同一 source でも adjustment 差異で新しい実行になる。
    """

    if analysis.source_hash != package.content_hash:
        raise ValueError("Static analysis is not bound to the normalized package")
    if analysis.blocked:
        raise UnsafeSkillSourceError("Skill source is blocked before interpreter execution")
    return {
        "request_version": "skillmind.skill-interpreter.request/v1",
        "source": {
            "content_hash": package.content_hash,
            "detected_adapter": package.detected_adapter,
            "normalized_package": package.to_dict(),
        },
        "static_analysis": analysis.to_dict(),
        "capability_catalog": catalog.to_dict(),
        "interpreter": system_skill.to_dict(),
        "target_contracts": {
            "runtime_manifest": "skillmind/v1alpha1",
            "task_contract_draft": "skillmind.task-contract-draft/v1",
            "view_spec": "skillmind.view/v1alpha1",
            "response": "skillmind.skill-interpreter.response/v1",
        },
        "previous_interpretation": dict(previous_interpretation)
        if previous_interpretation is not None
        else None,
        "adjustment": dict(adjustment) if adjustment is not None else None,
    }


def load_capability_catalog(path: Path) -> CapabilityCatalogSnapshot:
    """Versioned JSON から checksum 検証済み Tool catalog snapshot を読む。"""

    value = _load_json(path.resolve(strict=True))
    raw_capabilities = value.get("capabilities")
    if not isinstance(raw_capabilities, list):
        raise ValueError("Capability catalog capabilities must be an array")
    entries: list[CapabilityCatalogEntry] = []
    for raw in raw_capabilities:
        if not isinstance(raw, dict):
            raise ValueError("Capability catalog entry must be an object")
        providers = raw.get("providers")
        if not isinstance(providers, list) or not all(isinstance(item, str) for item in providers):
            raise ValueError("Capability catalog providers must be a string array")
        entries.append(
            CapabilityCatalogEntry(
                capability=_required_string(raw, "capability"),
                description=_required_string(raw, "description"),
                request_schema=_required_string(raw, "request_schema"),
                response_schema=_required_string(raw, "response_schema"),
                error_schema=_required_string(raw, "error_schema"),
                providers=tuple(providers),
            )
        )
    snapshot = CapabilityCatalogSnapshot.build(
        catalog_version=_required_string(value, "catalog_version"),
        capabilities=entries,
    )
    if snapshot.checksum != value.get("checksum"):
        raise ValueError("Capability catalog checksum does not match its content")
    return snapshot


class InterpreterFixtureRunner:
    """Model を呼ばず fixture response を本番同等 contract で検証する。"""

    def __init__(self, contracts_dir: Path) -> None:
        """S1 contract と RuntimeManifest Schema を読み込み Schema 自体も検証する。"""

        root = contracts_dir.resolve(strict=True)
        self._request_schema = _load_json(root / "skills/interpreter/v1/request.schema.json")
        self._response_schema = _load_json(root / "skills/interpreter/v1/response.schema.json")
        self._report_schema = _load_json(
            root / "skills/interpreter/v1/interpretation-report.schema.json"
        )
        self._analysis_schema = _load_json(
            root / "skills/interpreter/v1/static-analysis.schema.json"
        )
        self._catalog_schema = _load_json(
            root / "skills/interpreter/v1/capability-catalog.schema.json"
        )
        self._diagnostic_schema = _load_json(
            root / "skills/interpreter/v1/diagnostic.schema.json"
        )
        self._manifest_schema = _load_json(root / "runtime-manifest/v1alpha1.schema.json")
        self._blueprint_validator = CapabilityBlueprintValidator(root)
        for schema in (
            self._request_schema,
            self._response_schema,
            self._report_schema,
            self._analysis_schema,
            self._catalog_schema,
            self._diagnostic_schema,
            self._manifest_schema,
        ):
            Draft202012Validator.check_schema(schema)

    def run(
        self,
        request: Mapping[str, Any],
        fixture_response: Mapping[str, Any],
        *,
        bind_identity: bool = False,
    ) -> dict[str, Any]:
        """Schema と identity binding を検証し、defensive copy を返す。

        bind_identity=True (model 経路) は identity を platform 権威値で上書きする。model に
        interpreter_version/source_hash の複刻を強いない。False (fixture 経路) は従来どおり
        照合し、別 source/system Skill の結果混入を防ぐ。
        """

        checker = FormatChecker()
        request_value = copy.deepcopy(dict(request))
        response_value = copy.deepcopy(dict(fixture_response))
        Draft202012Validator(
            self._request_schema, format_checker=checker
        ).validate(request_value)
        analysis = _mapping(request_value, "static_analysis")
        catalog = _mapping(request_value, "capability_catalog")
        Draft202012Validator(self._analysis_schema, format_checker=checker).validate(analysis)
        Draft202012Validator(self._catalog_schema, format_checker=checker).validate(catalog)
        _validate_snapshot_checksum(analysis, "checksum", label="Static analysis")
        _validate_snapshot_checksum(catalog, "checksum", label="Capability catalog")
        if analysis.get("blocked") is True:
            raise UnsafeSkillSourceError("Blocked source cannot enter interpreter fixture runner")
        Draft202012Validator(
            self._response_schema, format_checker=checker
        ).validate(response_value)
        report = _mapping(response_value, "report")
        Draft202012Validator(self._report_schema, format_checker=checker).validate(report)
        for diagnostic in cast(list[dict[str, Any]], report.get("diagnostics", [])):
            Draft202012Validator(
                self._diagnostic_schema, format_checker=checker
            ).validate(diagnostic)
        manifest = _mapping(response_value, "runtime_manifest_draft")
        if bind_identity:
            _bind_interpreter_identity(request_value, response_value, manifest)
            _compile_model_task_contracts(manifest)
        manifest = normalize_runtime_manifest(manifest)
        response_value["runtime_manifest_draft"] = manifest
        Draft202012Validator(self._manifest_schema, format_checker=checker).validate(manifest)
        # 公開 manifest contract は blueprint を任意の object としてしか縛れない。宣言された
        # 蓝图は専用 contract と決定的規則で検証し、緩い envelope を素通りさせない。
        blueprint = manifest.get("capability_blueprint")
        if blueprint is not None:
            self._blueprint_validator.validate(cast(dict[str, Any], blueprint))
        if not bind_identity:
            _validate_fixture_identity(request_value, response_value, manifest)
        return response_value


def load_inline_text_files(
    root: Path, package: NormalizedSkillPackage
) -> tuple[InlineSkillFile, ...]:
    """Parser が index 済みの text file だけを offline runner 用に再読込する。"""

    resolved_root = root.resolve(strict=True)
    files: list[InlineSkillFile] = []
    for indexed in package.files:
        if indexed.binary:
            continue
        path = (resolved_root / indexed.path).resolve(strict=True)
        if not path.is_relative_to(resolved_root) or path.is_symlink():
            raise ValueError("Indexed Skill file escapes the source root")
        # source index の SHA-256 と同じ UTF-8 bytes を analyzer へ渡し、CRLF を LF へ変換しない。
        files.append(InlineSkillFile(path=indexed.path, content=path.read_bytes().decode("utf-8")))
    return tuple(files)


def _validated_source_texts(
    package: NormalizedSkillPackage,
    source_files: Sequence[InlineSkillFile],
) -> dict[str, str]:
    """Normalized file index と analyzer input の完全一致を検証する。"""

    expected = {item.path for item in package.files if not item.binary}
    index = {item.path: item for item in package.files if not item.binary}
    texts: dict[str, str] = {}
    for item in source_files:
        path = PurePosixPath(item.path)
        if path.is_absolute() or ".." in path.parts or item.path in texts:
            raise ValueError("Static analyzer source path is invalid or duplicated")
        encoded = item.content.encode("utf-8")
        indexed = index.get(item.path)
        if indexed is None or indexed.size != len(encoded) or indexed.sha256 != (
            f"sha256:{sha256_hex(encoded)}"
        ):
            raise ValueError("Static analyzer source content differs from the normalized index")
        texts[item.path] = item.content
    if set(texts) != expected:
        raise ValueError("Static analyzer input must match the normalized text file index")
    return texts


def _bind_interpreter_identity(
    request: Mapping[str, Any],
    response: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Model 出力に platform 権威の identity を上書きする(検証ではなく設定)。

    source_hash / interpreter_version は request(= platform)が知る事実であり、model の複刻に
    依存させない。interpretation_id は DRAFT 作成時に別途 stamp される。
    """

    source = _mapping(request, "source")
    interpreter = _mapping(request, "interpreter")
    content_hash = source.get("content_hash")
    response["source_hash"] = content_hash
    response["interpreter"] = dict(interpreter)
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        identity = {}
        manifest["identity"] = identity
    identity["source_hash"] = content_hash
    identity["interpreter_version"] = f"{interpreter.get('skill_key')}/{interpreter.get('version')}"
    bind_blueprint_identity(manifest)


def _compile_model_task_contracts(manifest: dict[str, Any]) -> None:
    """Model の TaskContractDraft を platform 権威の Schema と checksum へ変換する。

    新規 model 解釈では legacy ``$ref`` を受理しない。Schema と checksum は model の値を
    信用せず毎回上書きし、同じ draft が常に同じ実行契約へ凍結される境界を維持する。
    """

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("RuntimeManifest tasks must be an array")
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            raise ValueError(f"RuntimeManifest task must be an object: /tasks/{index}")
        input_contract = raw_task.get("input_contract")
        output_contract = raw_task.get("output_contract")
        if not isinstance(input_contract, Mapping):
            raise ValueError("Model RuntimeManifest tasks must define input_contract")
        if output_contract is not None and not isinstance(output_contract, Mapping):
            raise ValueError("Model RuntimeManifest output_contract must be an object")
        compiled_input = compile_task_contract(input_contract)
        raw_task["input_schema"] = compiled_input.schema
        raw_task["input_schema_checksum"] = compiled_input.checksum
        if isinstance(output_contract, Mapping):
            compiled_output = compile_task_contract(output_contract)
            raw_task["output_schema"] = compiled_output.schema
            raw_task["output_schema_checksum"] = compiled_output.checksum
        else:
            # 開放式 Outcome へ旧生成値が混入しないよう、model が宣言しなかった派生 field は除く。
            raw_task.pop("output_schema", None)
            raw_task.pop("output_schema_checksum", None)


def _validate_fixture_identity(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    """Fixture が別 source/system Skill の結果を混入しないよう identity を照合する。"""

    source = _mapping(request, "source")
    interpreter = _mapping(request, "interpreter")
    response_interpreter = _mapping(response, "interpreter")
    identity = _mapping(manifest, "identity")
    expected_version = f"{interpreter.get('skill_key')}/{interpreter.get('version')}"
    if response.get("source_hash") != source.get("content_hash"):
        raise ValueError("Interpreter fixture source hash does not match request")
    for key in ("skill_key", "version", "prompt_checksum"):
        if response_interpreter.get(key) != interpreter.get(key):
            raise ValueError("Interpreter fixture system Skill identity does not match request")
    if identity.get("source_hash") != source.get("content_hash"):
        raise ValueError("RuntimeManifest source hash does not match request")
    if identity.get("interpreter_version") != expected_version:
        raise ValueError("RuntimeManifest interpreter version does not match request")


def _validate_snapshot_checksum(
    value: Mapping[str, Any], checksum_key: str, *, label: str
) -> None:
    """Snapshot の checksum を field 自身を除いた canonical JSON と照合する。"""

    expected = value.get(checksum_key)
    body = {key: copy.deepcopy(item) for key, item in value.items() if key != checksum_key}
    actual = f"sha256:{sha256_hex(canonical_json(body))}"
    if expected != actual:
        raise ValueError(f"{label} checksum does not match its content")


def _load_json(path: Path) -> dict[str, Any]:
    """Contract path から JSON object だけを読み込む。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Contract must be a JSON object: {path}")
    return cast(dict[str, Any], value)


def _materialize_local_refs(
    schema: Mapping[str, Any], *, reference_root: str = "#", bound_task_contracts: bool = False
) -> dict[str, Any]:
    """Local ref を展開し、有界 contract の共有定義を最終 response 内へ対応付ける。"""

    definitions = schema.get("$defs")
    defs = cast(dict[str, Any], definitions) if isinstance(definitions, dict) else {}
    retained_definitions: frozenset[str] = frozenset()
    if bound_task_contracts:
        defs, retained_definitions = _bounded_task_contract_definitions(defs)
    definition_reference_found = False

    def resolve(value: Any, stack: tuple[str, ...] = ()) -> Any:
        """通常 ref を展開し、共有 contract 定義と汎用の再帰参照はそのまま残す。"""

        nonlocal definition_reference_found

        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                key = reference.removeprefix("#/$defs/")
                if key not in defs:
                    raise ValueError("Interpreter generation Schema has an invalid local reference")
                if key in retained_definitions or key in stack:
                    # 同じ残深度の field/items を共有し、分岐ごとの指数的な展開を避ける。
                    definition_reference_found = True
                    return copy.deepcopy(value)
                resolved = resolve(defs[key], (*stack, key))
                if not isinstance(resolved, dict):
                    raise ValueError("Interpreter generation Schema definition must be an object")
                siblings = {name: item for name, item in value.items() if name != "$ref"}
                expanded_siblings = resolve(siblings, stack)
                if not isinstance(expanded_siblings, dict):  # pragma: no cover - dict input.
                    raise ValueError("Interpreter generation Schema siblings must be an object")
                return {**resolved, **expanded_siblings}
            return {
                name: resolve(item, stack)
                for name, item in value.items()
                if name not in {"$schema", "$id", "$defs"}
            }
        if isinstance(value, list):
            return [resolve(item, stack) for item in value]
        return copy.deepcopy(value)

    materialized = resolve(dict(schema))
    if not isinstance(materialized, dict):  # pragma: no cover - root type is fixed above.
        raise ValueError("Interpreter generation Schema must be an object")
    if definition_reference_found:
        materialized["$defs"] = copy.deepcopy(defs)
        _relocate_generation_refs(materialized, reference_root)
    return cast(dict[str, Any], materialized)


def _bounded_task_contract_definitions(
    definitions: Mapping[str, Any],
) -> tuple[dict[str, Any], frozenset[str]]:
    """根を 1、field/items を各 +1 として compiler と同じ最大深度の定義を共有する。"""

    templates = {
        key: _mapping(definitions, key)
        for key in ("taskContractDraft", "contractField", "contractItem")
    }
    value_types = _mapping(definitions, "contractValueType").get("enum")
    if not isinstance(value_types, list):
        raise ValueError("Task contract generation types must be an enum")
    bounded = {
        key: copy.deepcopy(value) for key, value in definitions.items() if key not in templates
    }
    retained: set[str] = set()
    for depth in range(1, MAX_CONTRACT_DEPTH + 1):
        kinds = ("taskContractDraft",) if depth == 1 else ("contractField", "contractItem")
        for kind in kinds:
            node = copy.deepcopy(templates[kind])
            properties = _mapping(node, "properties")
            fields = _mapping(properties, "fields")
            if depth == MAX_CONTRACT_DEPTH:
                # 空 object はこの深度でも有効。array は必須 items が次の深度へ進むため不可。
                fields["maxItems"] = 0
                fields.pop("items", None)
                properties.pop("items", None)
                properties["type"] = {"enum": [value for value in value_types if value != "array"]}
            else:
                fields["items"] = {"$ref": f"#/$defs/contractFieldDepth{depth + 1}"}
                properties["items"] = {"$ref": f"#/$defs/contractItemDepth{depth + 1}"}
            name = kind if depth == 1 else f"{kind}Depth{depth}"
            bounded[name] = node
            retained.add(name)
    return bounded, frozenset(retained)


def _relocate_generation_refs(value: Any, reference_root: str) -> None:
    """埋込先の `$defs` と内部参照を揃え、Manifest/Blueprint の同名定義を混在させない。"""

    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            value["$ref"] = reference_root + reference[1:]
        for item in value.values():
            _relocate_generation_refs(item, reference_root)
    elif isinstance(value, list):
        for item in value:
            _relocate_generation_refs(item, reference_root)


def _restrict_manifest_to_model_contract_drafts(manifest: dict[str, Any]) -> None:
    """Generation Schema から compiler 出力と legacy contract ref を除外する。

    RuntimeManifest Schema は既存 Release A の読み取り互換性を保つ。一方、新規 model 出力は
    TaskContractDraft と source trace だけを提案し、実行可能 Schema を直接生成できない。
    """

    properties = manifest.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("RuntimeManifest generation Schema properties must be an object")
    tasks = properties.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("RuntimeManifest generation Schema tasks must be an object")
    task = tasks.get("items")
    if not isinstance(task, dict):
        raise ValueError("RuntimeManifest generation Schema task item must be an object")
    task.pop("oneOf", None)
    # Model は compiler 派生 field を出さないため、公開 Manifest contract の dependency は
    # generation Schema から外す。出力後に platform が生成値を補完してから再検証する。
    task.pop("dependentRequired", None)
    task["required"] = [
        "key",
        "input_contract",
        "contract_source_trace",
    ]
    task_properties = task.get("properties")
    if not isinstance(task_properties, dict):
        raise ValueError("RuntimeManifest generation task properties must be an object")
    for key in (
        "input_schema",
        "output_schema",
        "input_schema_checksum",
        "output_schema_checksum",
    ):
        task_properties.pop(key, None)


def _restrict_blueprint_to_model_fields(blueprint: dict[str, Any]) -> None:
    """Generation Schema から platform が束縛する blueprint field を除外する。

    identity と compatibility は request(= platform)が知る事実であり、model に複刻させると
    源 hash や互換 level の食い違いが生じる。RuntimeManifest 側と二重管理にしないため、
    model には能力・目標・資源・指導・効果だけを生成させる。
    """

    properties = blueprint.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("CapabilityBlueprint generation Schema properties must be an object")
    for key in ("identity", "compatibility"):
        properties.pop(key, None)
    required = blueprint.get("required")
    if not isinstance(required, list):
        raise ValueError("CapabilityBlueprint generation Schema must declare required fields")
    blueprint["required"] = [
        key for key in required if key not in {"identity", "compatibility"}
    ]


def _require_model_capability_blueprint(
    manifest: dict[str, Any], blueprint: dict[str, Any]
) -> None:
    """Model の RuntimeManifest へ能力蓝图を必須 field として埋め込む。

    公開 manifest contract では blueprint を任意の object に留め、旧 manifest の読み取り
    互換を壊さない。一方、新規 model 解釈は蓝图を主産物とするため、生成 Schema 側でのみ
    完全な形状と必須性を課す。
    """

    properties = manifest.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("RuntimeManifest generation Schema properties must be an object")
    properties["capability_blueprint"] = blueprint
    required = manifest.get("required")
    if not isinstance(required, list):
        raise ValueError("RuntimeManifest generation Schema must declare required fields")
    if "capability_blueprint" not in required:
        manifest["required"] = [*required, "capability_blueprint"]


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """必須 object field を型付きで取得する。"""

    nested = value.get(key)
    if not isinstance(nested, dict):
        raise ValueError(f"Interpreter contract field must be an object: {key}")
    return cast(dict[str, Any], nested)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    """Contract fixture の必須 non-empty string を取得する。"""

    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"Interpreter contract field must be a non-empty string: {key}")
    return item


def _diagnostic_sort_key(item: StaticAnalysisDiagnostic) -> tuple[str, int, str, str]:
    """Diagnostic 順を path、line、severity、code で安定化する。"""

    return (item.path or "", item.line or 0, item.severity, item.code)


def _located_sort_key(item: Mapping[str, Any]) -> tuple[str, int, str]:
    """Location 付き finding を安定順化する。"""

    return (str(item.get("path", "")), int(item.get("line") or 0), str(item.get("language", "")))


def _unsupported_sort_key(item: Mapping[str, Any]) -> tuple[str, int, str]:
    """Unsupported operation を安定順化する。"""

    return (str(item.get("path", "")), int(item.get("line") or 0), str(item.get("code", "")))
