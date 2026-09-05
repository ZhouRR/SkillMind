"""Directory 式 Skill を script 実行なしで安全に正規化する。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from yaml.events import AliasEvent

from projectmind.skills.task_contract import compile_task_contract

_FRONT_MATTER_BOUNDARY = "---"
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_KEY_CLEANUP = re.compile(r"[^a-z0-9.-]+")
_KNOWN_METADATA_FIELDS = frozenset(
    {
        "name",
        "description",
        "argument-hint",
        "argument_hint",
        "allowed-tools",
        "allowed_tools",
    }
)


class SkillImportError(ValueError):
    """Source を不変 package として受理できない理由を表す。"""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        """Credential や file 内容を含まない安定 code と定位を保持する。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


@dataclass(frozen=True, slots=True)
class SkillImportLimits:
    """Archive 展開後にも適用できる directory import 上限。"""

    max_files: int = 1_000
    max_file_bytes: int = 2_000_000
    max_total_bytes: int = 20_000_000
    max_markdown_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        """DoS 防止の上限を無制限化する値を拒否する。"""

        if (
            min(
                self.max_files,
                self.max_file_bytes,
                self.max_total_bytes,
                self.max_markdown_bytes,
            )
            <= 0
        ):
            raise ValueError("Skill import limits must be positive")


@dataclass(frozen=True, slots=True)
class SkillFile:
    """Source 内の一つの不変 file index entry。"""

    path: str
    mime: str
    size: int
    sha256: str
    binary: bool

    def to_dict(self) -> dict[str, Any]:
        """Storage/API に依存しない JSON object を返す。"""

        return {
            "path": self.path,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "binary": self.binary,
        }


@dataclass(frozen=True, slots=True)
class InstructionSection:
    """Primary Markdown から決定的に分割した heading section。"""

    title: str
    level: int
    line: int

    def to_dict(self) -> dict[str, Any]:
        """Interpreter 入力用の最小 section metadata を返す。"""

        return {"title": self.title, "level": self.level, "line": self.line}


@dataclass(frozen=True, slots=True)
class ImportDiagnostic:
    """Import を継続できる warning/info と発生箇所。"""

    severity: str
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """RuntimeManifest にも複製できる diagnostic object を返す。"""

        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True, slots=True)
class SkillResources:
    """File index を script/reference/asset 用途で分類した view。"""

    scripts: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        """Resource path を安定順の JSON list で返す。"""

        return {
            "scripts": list(self.scripts),
            "references": list(self.references),
            "assets": list(self.assets),
        }


@dataclass(frozen=True, slots=True)
class NormalizedSkillPackage:
    """Adapter 差を吸収した projectmind.normalized/v1 package。"""

    source_type: str
    content_hash: str
    detected_adapter: str
    name: str
    description: str
    argument_hint: str | None
    primary_instruction: str
    instruction_text: str
    sections: tuple[InstructionSection, ...]
    files: tuple[SkillFile, ...]
    resources: SkillResources
    declared_tools: tuple[str, ...]
    extensions: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[ImportDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical persistence と checksum 検証に利用できる JSON object を返す。"""

        return {
            "package_format": "projectmind.normalized/v1",
            "source": {
                "type": self.source_type,
                "content_hash": self.content_hash,
                "detected_adapter": self.detected_adapter,
                "files": [file.to_dict() for file in self.files],
            },
            "metadata": {
                "name": self.name,
                "description": self.description,
                "argument_hint": self.argument_hint,
            },
            "instructions": {
                "primary": self.primary_instruction,
                "text": self.instruction_text,
                "sections": [section.to_dict() for section in self.sections],
            },
            "resources": self.resources.to_dict(),
            "declared_tools": list(self.declared_tools),
            "extensions": _json_copy(dict(self.extensions)),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class _ScannedSource:
    """Adapter 選択前の root、file index、テキストを保持する。"""

    root: Path
    files: tuple[SkillFile, ...]
    text_files: Mapping[str, str]
    content_hash: str


class SkillAdapter(Protocol):
    """Scanned source を共通 package へ変換する deterministic port。"""

    adapter_id: str

    def supports(self, source: _ScannedSource) -> bool:
        """Source shape を外部実行なしで判定する。"""

        ...

    def normalize(self, source: _ScannedSource) -> NormalizedSkillPackage:
        """Scanned file 以外を読まずに package を構築する。"""

        ...


class DirectorySkillAdapter:
    """SKILL.md と YAML front matter を Codex/Claude Code 共通形式として読む。"""

    adapter_id = "directory-skill/v1"

    def supports(self, source: _ScannedSource) -> bool:
        """Root 直下の SKILL.md だけを directory Skill の entry point とする。"""

        return "SKILL.md" in source.text_files

    def normalize(self, source: _ScannedSource) -> NormalizedSkillPackage:
        """Known metadata を正規化し、未知 field と Tool 声明を隔離する。"""

        raw = source.text_files["SKILL.md"]
        metadata, body = _parse_front_matter(raw, path="SKILL.md")
        name = _metadata_string(metadata, "name") or _first_heading(body) or source.root.name
        description = _metadata_string(metadata, "description") or _first_paragraph(body)
        argument_hint = _metadata_string(metadata, "argument-hint", "argument_hint")
        declared_tools = _metadata_string_list(metadata, "allowed-tools", "allowed_tools")
        extensions = {
            str(key): _json_copy(value)
            for key, value in metadata.items()
            if str(key) not in _KNOWN_METADATA_FIELDS
        }
        diagnostics: list[ImportDiagnostic] = []
        if declared_tools:
            diagnostics.append(
                ImportDiagnostic(
                    severity="warning",
                    code="declared_tools_not_authorized",
                    message="Source Tool declarations were recorded but did not grant permissions",
                    path="SKILL.md",
                )
            )
        return _normalized_package(
            source,
            adapter_id=self.adapter_id,
            primary="SKILL.md",
            name=name,
            description=description,
            argument_hint=argument_hint,
            body=body,
            declared_tools=declared_tools,
            extensions=extensions,
            diagnostics=tuple(diagnostics),
        )


class GenericDocumentAdapter:
    """SKILL.md がない Markdown directory を Assisted 候補へ正規化する。"""

    adapter_id = "generic-document/v1"

    def supports(self, source: _ScannedSource) -> bool:
        """README.md または一つ以上の Markdown file がある source を受理する。"""

        return any(path.lower().endswith(".md") for path in source.text_files)

    def normalize(self, source: _ScannedSource) -> NormalizedSkillPackage:
        """README を優先し、なければ辞書順最初の Markdown を entry とする。"""

        markdown = sorted(path for path in source.text_files if path.lower().endswith(".md"))
        primary = "README.md" if "README.md" in markdown else markdown[0]
        body = source.text_files[primary]
        return _normalized_package(
            source,
            adapter_id=self.adapter_id,
            primary=primary,
            name=_first_heading(body) or source.root.name,
            description=_first_paragraph(body),
            argument_hint=None,
            body=body,
            declared_tools=(),
            extensions={},
            diagnostics=(
                ImportDiagnostic(
                    severity="warning",
                    code="generic_document_assisted_only",
                    message=(
                        "Generic document requires interpretation before executable publication"
                    ),
                    path=primary,
                ),
            ),
        )


class SkillPackageParser:
    """Secure scanner と順序付き trusted Adapter registry を統合する。"""

    def __init__(
        self,
        *,
        limits: SkillImportLimits | None = None,
        adapters: Sequence[SkillAdapter] | None = None,
    ) -> None:
        """Import 上限と platform 管理 Adapter の検出順を固定する。"""

        self._limits = limits or SkillImportLimits()
        self._adapters = tuple(adapters or (DirectorySkillAdapter(), GenericDocumentAdapter()))

    def parse_directory(self, root: Path) -> NormalizedSkillPackage:
        """Directory を一度だけ scan し、最初に match した Adapter で正規化する。"""

        source = _scan_directory(root, self._limits)
        _validate_markdown_references(source)
        for adapter in self._adapters:
            if adapter.supports(source):
                return adapter.normalize(source)
        raise SkillImportError(
            "unsupported_skill_source",
            "Directory does not contain a supported Skill or Markdown entry point",
        )


class DeterministicManifestDraftBuilder:
    """Model 推測を行わず Assisted RuntimeManifest Draft を生成する。"""

    def __init__(self, manifest_schema: Mapping[str, Any]) -> None:
        """Versioned RuntimeManifest Schema を事前検証して保持する。"""

        Draft202012Validator.check_schema(manifest_schema)
        self._schema = dict(manifest_schema)

    def build(self, package: NormalizedSkillPackage) -> dict[str, Any]:
        """Source hash から identity/key を決定し、検証済み draft を返す。"""

        skill_key = _skill_key(package.name, package.content_hash)
        diagnostics = [diagnostic.to_dict() for diagnostic in package.diagnostics]
        diagnostics.append(
            ImportDiagnostic(
                severity="warning",
                code="interpreter_required",
                message="Deterministic parsing cannot infer executable business contracts",
            ).to_dict()
        )
        input_contract = {
            "contract_version": "projectmind.task-contract-draft/v1",
            "type": "object",
            "description": "Inputs require model interpretation",
            "fields": [],
        }
        output_contract = {
            "contract_version": "projectmind.task-contract-draft/v1",
            "type": "object",
            "description": "Result requires model interpretation",
            "fields": [],
        }
        compiled_input = compile_task_contract(input_contract)
        compiled_output = compile_task_contract(output_contract)
        manifest: dict[str, Any] = {
            "manifest_version": "projectmind/v1alpha1",
            "identity": {
                "skill_key": skill_key,
                "source_hash": package.content_hash,
                "interpretation_id": str(
                    uuid5(NAMESPACE_URL, f"projectmind:deterministic:{package.content_hash}")
                ),
                "interpreter_version": "deterministic-parser/1.0.0",
            },
            "compatibility": {
                "level": "assisted",
                "confidence": 0.25,
                "diagnostics": diagnostics,
            },
            "capabilities": [
                {
                    "key": f"skill.{skill_key}.assist",
                    "title": package.name,
                    "triggers": [],
                }
            ],
            "tasks": [
                {
                    "key": "assist",
                    "capability": f"skill.{skill_key}.assist",
                    "type": "immediate",
                    "input_contract": input_contract,
                    "output_contract": output_contract,
                    "input_schema": compiled_input.schema,
                    "output_schema": compiled_output.schema,
                    "input_schema_checksum": compiled_input.checksum,
                    "output_schema_checksum": compiled_output.checksum,
                    "contract_source_trace": [
                        {
                            "contract": "input",
                            "field_path": "/",
                            "source_path": "SKILL.md",
                            "source_section": None,
                            "line": 1,
                        },
                        {
                            "contract": "output",
                            "field_path": "/",
                            "source_path": "SKILL.md",
                            "source_section": None,
                            "line": 1,
                        },
                    ],
                    "workflow": "assist-v1",
                    "view": "standard",
                }
            ],
            "tools": [],
            "workflows": [{"key": "assist-v1", "steps": [{"key": "guide", "kind": "agent"}]}],
            "permissions": {
                "default_tool_policy": "auto",
                "registered_script_policy": "auto",
                "external_write_policy": "deny",
                "write_capabilities": [],
                "network_scope": "project_integrations_only",
            },
            "ui": {
                "default_view": "standard",
                "views": [],
                "frontend_module": None,
            },
            "tests": [],
            "extensions": {
                "normalized_package_hash": _package_hash(package),
                "declared_tools": list(package.declared_tools),
            },
        }
        errors = list(
            Draft202012Validator(self._schema, format_checker=FormatChecker()).iter_errors(manifest)
        )
        if errors:
            raise RuntimeError("Deterministic RuntimeManifest Draft failed schema validation")
        return manifest


def _scan_directory(root: Path, limits: SkillImportLimits) -> _ScannedSource:
    """Symlink を追跡せず、安定順 file index と source hash を作成する。"""

    if not root.is_absolute():
        raise SkillImportError("invalid_source_root", "Skill source root must be absolute")
    if root.is_symlink() or not root.is_dir():
        raise SkillImportError("invalid_source_root", "Skill source root must be a directory")
    root = root.absolute()
    paths: list[Path] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            candidate = current_path / name
            if candidate.is_symlink():
                raise SkillImportError(
                    "symlink_not_allowed",
                    "Skill source must not contain symbolic links",
                    path=candidate.relative_to(root).as_posix(),
                )
        for name in file_names:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise SkillImportError(
                    "symlink_not_allowed",
                    "Skill source must not contain symbolic links",
                    path=relative,
                )
            paths.append(candidate)
    paths.sort(key=lambda path: path.relative_to(root).as_posix())
    if not paths:
        raise SkillImportError("empty_skill_source", "Skill source directory is empty")
    if len(paths) > limits.max_files:
        raise SkillImportError("too_many_files", "Skill source contains too many files")

    files: list[SkillFile] = []
    text_files: dict[str, str] = {}
    total_bytes = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > limits.max_file_bytes:
            raise SkillImportError(
                "file_too_large", "Skill source file exceeds the size limit", path=relative
            )
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise SkillImportError("package_too_large", "Skill source exceeds the total size limit")
        raw = path.read_bytes()
        binary = _is_binary(raw)
        if binary and relative.lower().endswith(".md"):
            raise SkillImportError(
                "invalid_text_encoding",
                "Markdown Skill files must use UTF-8 text",
                path=relative,
            )
        mime = _stable_mime_type(relative, binary=binary)
        files.append(
            SkillFile(
                path=relative,
                mime=mime,
                size=size,
                sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
                binary=binary,
            )
        )
        if not binary:
            if relative.lower().endswith(".md") and size > limits.max_markdown_bytes:
                raise SkillImportError(
                    "markdown_too_large",
                    "Markdown instruction exceeds the size limit",
                    path=relative,
                )
            try:
                text_files[relative] = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SkillImportError(
                    "invalid_text_encoding", "Text Skill files must use UTF-8", path=relative
                ) from error
    index = [file.to_dict() for file in files]
    canonical = json.dumps(index, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _ScannedSource(
        root=root,
        files=tuple(files),
        text_files=text_files,
        content_hash=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    )


def _validate_markdown_references(source: _ScannedSource) -> None:
    """Local Markdown reference の欠落、root 逃逸、循環を import 時に拒否する。"""

    known_paths = {file.path for file in source.files}
    graph: dict[str, set[str]] = {}
    for path, text in source.text_files.items():
        if not path.lower().endswith(".md"):
            continue
        references: set[str] = set()
        for match in _MARKDOWN_LINK.finditer(text):
            target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            if parsed.path.startswith("/"):
                raise SkillImportError(
                    "reference_escape",
                    "Markdown reference escapes the Skill source root",
                    path=path,
                )
            decoded = unquote(parsed.path)
            relative = PurePosixPath(path).parent / PurePosixPath(decoded)
            normalized = _normalize_relative_path(relative, owner=path)
            if normalized not in known_paths:
                raise SkillImportError(
                    "missing_reference",
                    "Markdown reference does not exist in the Skill source",
                    path=path,
                )
            if normalized.lower().endswith(".md"):
                references.add(normalized)
        graph[path] = references
    _reject_reference_cycles(graph)


def _normalize_relative_path(path: PurePosixPath, *, owner: str) -> str:
    """Pure path の dot segment を解決し、root より上へ出る参照を拒否する。"""

    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise SkillImportError(
                    "reference_escape",
                    "Markdown reference escapes the Skill source root",
                    path=owner,
                )
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts).as_posix()


def _reject_reference_cycles(graph: Mapping[str, set[str]]) -> None:
    """Markdown 相互参照による Interpreter 入力の無限展開を防ぐ。"""

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        """Depth-first search で back edge を検出する。"""

        if node in visiting:
            raise SkillImportError(
                "reference_cycle", "Markdown references contain a cycle", path=node
            )
        if node in visited:
            return
        visiting.add(node)
        for target in graph.get(node, set()):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)


# PyYAML は type stub を提供しないため、runtime で確実な SafeLoader 継承だけを局所的に許可する。
class _NoAliasSafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """YAML alias expansion を禁止した front matter 専用 loader。"""

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Alias event を object 展開前に拒否する。"""

        if self.check_event(AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed")
        return super().compose_node(parent, index)


def _parse_front_matter(text: str, *, path: str) -> tuple[dict[str, Any], str]:
    """Optional YAML front matter と Markdown body を副作用なしで分離する。"""

    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_BOUNDARY:
        return {}, text
    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == _FRONT_MATTER_BOUNDARY
        )
    except StopIteration as error:
        raise SkillImportError(
            "invalid_front_matter", "YAML front matter is not closed", path=path
        ) from error
    raw_metadata = "\n".join(lines[1:closing])
    try:
        loaded = yaml.load(raw_metadata, Loader=_NoAliasSafeLoader) if raw_metadata else {}
    except yaml.YAMLError as error:
        raise SkillImportError(
            "invalid_front_matter", "YAML front matter is invalid", path=path
        ) from error
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise SkillImportError(
            "invalid_front_matter", "YAML front matter must be a string-keyed object", path=path
        )
    return dict(loaded), "\n".join(lines[closing + 1 :]).lstrip("\n")


def _normalized_package(
    source: _ScannedSource,
    *,
    adapter_id: str,
    primary: str,
    name: str,
    description: str,
    argument_hint: str | None,
    body: str,
    declared_tools: tuple[str, ...],
    extensions: Mapping[str, Any],
    diagnostics: tuple[ImportDiagnostic, ...],
) -> NormalizedSkillPackage:
    """Adapter 共通の section/resource 構築と immutable copy を行う。"""

    return NormalizedSkillPackage(
        source_type="directory",
        content_hash=source.content_hash,
        detected_adapter=adapter_id,
        name=name[:200],
        description=description[:2_000],
        argument_hint=argument_hint[:500] if argument_hint else None,
        primary_instruction=primary,
        instruction_text=body,
        sections=_sections(body),
        files=source.files,
        resources=_resources(source.files),
        declared_tools=declared_tools,
        extensions=_json_copy(dict(extensions)),
        diagnostics=diagnostics,
    )


def _sections(body: str) -> tuple[InstructionSection, ...]:
    """Markdown heading を line 順の section metadata へ変換する。"""

    sections: list[InstructionSection] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        match = _HEADING.match(line)
        if match:
            sections.append(
                InstructionSection(
                    title=match.group(2).strip(),
                    level=len(match.group(1)),
                    line=line_number,
                )
            )
    return tuple(sections)


def _resources(files: tuple[SkillFile, ...]) -> SkillResources:
    """Conventional top-level directory だけを resource class へ映射する。"""

    scripts: list[str] = []
    references: list[str] = []
    assets: list[str] = []
    for file in files:
        top = PurePosixPath(file.path).parts[0].lower()
        if top == "scripts":
            scripts.append(file.path)
        elif top == "references":
            references.append(file.path)
        elif top == "assets":
            assets.append(file.path)
    return SkillResources(tuple(scripts), tuple(references), tuple(assets))


def _metadata_string(metadata: Mapping[str, Any], *keys: str) -> str | None:
    """Known metadata alias の最初の non-empty string を返す。"""

    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _metadata_string_list(metadata: Mapping[str, Any], *keys: str) -> tuple[str, ...]:
    """Tool 声明の CSV または YAML list を安定順で正規化する。"""

    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(item.strip() for item in value if item.strip())
    return ()


def _first_heading(body: str) -> str | None:
    """Document title 候補となる最初の Markdown heading を返す。"""

    for line in body.splitlines():
        match = _HEADING.match(line)
        if match:
            return match.group(2).strip()
    return None


def _first_paragraph(body: str) -> str:
    """Heading/code fence 以外の最初の短い段落を description として返す。"""

    paragraph: list[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or _HEADING.match(line):
            continue
        if not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)[:2_000]


def _skill_key(name: str, source_hash: str) -> str:
    """Unicode name も安定した Manifest key へ変換する。"""

    candidate = _KEY_CLEANUP.sub("-", name.lower()).strip("-.")
    if not candidate or not candidate[0].isalpha():
        candidate = f"imported-{source_hash.removeprefix('sha256:')[:12]}"
    return candidate[:80].rstrip("-.")


def _package_hash(package: NormalizedSkillPackage) -> str:
    """Normalized representation 自体の canonical SHA-256 を返す。"""

    canonical = json.dumps(
        package.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _is_binary(raw: bytes) -> bool:
    """NUL または UTF-8 decode 不可の file を binary として扱う。"""

    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _stable_mime_type(path: str, *, binary: bool) -> str:
    """OS 固有 MIME registry に依存しない限定 mapping を返す。"""

    suffix = PurePosixPath(path).suffix.lower()
    known = {
        ".css": "text/css",
        ".csv": "text/csv",
        ".gif": "image/gif",
        ".html": "text/html",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".js": "text/javascript",
        ".json": "application/json",
        ".md": "text/markdown",
        ".png": "image/png",
        ".py": "text/x-python",
        ".svg": "image/svg+xml",
        ".toml": "application/toml",
        ".ts": "text/typescript",
        ".txt": "text/plain",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }
    return known.get(suffix, "application/octet-stream" if binary else "text/plain")


def _json_copy(value: Any) -> Any:
    """YAML 由来値を JSON-compatible な defensive copy へ制限する。"""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise SkillImportError(
            "metadata_not_json", "Skill metadata must be JSON compatible"
        ) from error
