"""Skill source import と interpretation preview の domain object を定義する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class SkillInterpretationStatus(StrEnum):
    """SkillInterpretation の追加式 lifecycle state。"""

    ANALYZING = "ANALYZING"
    PREVIEW_READY = "PREVIEW_READY"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class SkillVersionStatus(StrEnum):
    """SkillVersion の publish lifecycle state。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    DEPRECATED = "DEPRECATED"


@dataclass(frozen=True, slots=True)
class InlineSkillFile:
    """API から受け取る source root 内の一つの UTF-8 text file。"""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class UploadSkillFile:
    """Multipart upload で受け取る source root 内の一つの file (binary 可)。"""

    path: str
    data: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class SkillPreview:
    """永続化前後で共有する deterministic parser の出力。"""

    normalized_package: dict[str, Any]
    runtime_manifest_draft: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SaveSkillPreviewCommand:
    """SkillSource と deterministic interpretation を保存する command。"""

    organization_id: UUID
    imported_by: UUID
    name: str
    source_type: str
    source_hash: str
    source_files: tuple[InlineSkillFile, ...]
    interpreter_version: str
    compatibility_level: str
    confidence: float
    diagnostics: tuple[dict[str, Any], ...]
    checksum: str
    preview: SkillPreview
    # None の場合は SkillSource id 由来の database:// URI を使う (inline 既定・後方互換)。
    # Upload 経路は object storage に bundle を保存済みの s3:// URI をここへ固定する。
    storage_uri: str | None = None
    # 元の bytes を object storage から再構築するため、全 file の index (binary を含む) を保持する。
    source_file_index: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class StoredSkillSource:
    """Interpret 実行の入力に使う保存済み SkillSource snapshot。"""

    skill_source_id: UUID
    organization_id: UUID
    name: str
    source_type: str
    source_hash: str
    source_files: tuple[InlineSkillFile, ...]
    storage_uri: str = ""
    source_file_index: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SaveModelInterpretationCommand:
    """Model interpretation 実行の成功/失敗を不変 record として保存する command。"""

    organization_id: UUID
    skill_source_id: UUID
    execution_key: str
    interpreter_version: str
    model: str
    status: SkillInterpretationStatus
    compatibility_level: str
    confidence: float
    summary: str
    assumptions: tuple[dict[str, Any], ...]
    questions: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    normalized_package: dict[str, Any]
    manifest_draft: dict[str, Any]
    report: dict[str, Any] | None
    execution: dict[str, Any]
    parent_interpretation_id: UUID | None = None
    adjustment: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StoredInterpretationExecution:
    """Model interpretation 実行結果の公開 read model。"""

    interpretation_id: UUID
    skill_source_id: UUID
    organization_id: UUID
    status: SkillInterpretationStatus
    origin: str
    model: str | None
    interpreter_version: str
    execution_key: str | None
    error_code: str | None
    compatibility_level: str
    confidence: float
    summary: str
    created_at: datetime
    preview: SkillPreview
    report: dict[str, Any] | None
    reused: bool
    parent_interpretation_id: UUID | None = None
    adjustment: dict[str, Any] | None = None
    diff: dict[str, Any] = field(default_factory=dict)
    validation_attempts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredSkillPreview:
    """保存済み SkillSource と SkillInterpretation の公開 read model。"""

    skill_source_id: UUID
    interpretation_id: UUID
    organization_id: UUID
    name: str
    source_hash: str
    source_type: str
    interpretation_status: SkillInterpretationStatus
    compatibility_level: str
    confidence: float
    interpreter_version: str
    created_at: datetime
    preview: SkillPreview


@dataclass(frozen=True, slots=True)
class ManifestGateFinding:
    """Manifest publish gate の一つの決定的判定。"""

    code: str
    severity: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class StoredSkillVersion:
    """Source、Interpretation、Manifest を精確に固定した version read model。"""

    skill_id: UUID
    skill_version_id: UUID
    skill_source_id: UUID
    interpretation_id: UUID
    organization_id: UUID
    skill_key: str
    name: str
    # SKILL.md の description。利用者向け一覧は skill_key より原文の説明のほうが読める。
    description: str
    version: str
    status: SkillVersionStatus
    manifest_checksum: str
    manifest: dict[str, Any]
    gate_passed: bool
    gate_findings: tuple[ManifestGateFinding, ...]
    interpretation_diff: dict[str, Any]
    created_at: datetime
    published_by: UUID | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreateSkillVersionDraftCommand:
    """Interpretation から frozen DRAFT を作成する persistence command。"""

    organization_id: UUID
    interpretation_id: UUID
    manifest: dict[str, Any]
    manifest_checksum: str
    gate_passed: bool
    gate_findings: tuple[ManifestGateFinding, ...]
    interpretation_diff: dict[str, Any]


class SkillSourceNotFoundError(LookupError):
    """指定 Organization から参照可能な SkillSource が存在しないことを表す。"""


class SkillInterpreterUnavailableError(RuntimeError):
    """Model interpreter が未配線のまま interpret が要求されたことを表す。"""


class SkillStorageUnavailableError(RuntimeError):
    """Upload または保存済み source の再構築に object storage が必要なことを表す。"""


class SkillSourceIntegrityError(RuntimeError):
    """保存済み SkillSource の file manifest または object checksum が不整合なことを表す。"""


class SkillInterpretationNotReadyError(ValueError):
    """PREVIEW_READY でない interpretation から発行操作が要求されたことを表す。"""


class SkillInterpretationNotFoundError(LookupError):
    """指定 SkillInterpretation が存在しないことを表す。"""


class SkillVersionNotFoundError(LookupError):
    """指定 Organization または Project から参照可能な SkillVersion がないことを表す。"""


class SkillVersionEnablementConflictError(ValueError):
    """Project への有効化/停用が version 状態または監査 lifecycle と両立しないことを表す。"""


class SkillVersionEnablementNotFoundError(LookupError):
    """Project に active な SkillVersion 有効化関係が存在しないことを表す。"""


class SkillPublishGateError(ValueError):
    """Hard gate 未通過の SkillVersion に publish が要求されたことを表す。"""


def validate_skill_publication(
    *,
    report: object,
    evaluation: tuple[bool, tuple[ManifestGateFinding, ...]],
    accepted_warnings: frozenset[str],
) -> tuple[ManifestGateFinding, ...]:
    """保存時と現在の門禁を共に満たし、明示受理済みの finding だけを発行へ渡す。"""

    message = "SkillVersion publish gate has unresolved findings"
    if not isinstance(report, dict) or type(report.get("passed")) is not bool:
        raise SkillPublishGateError(message)
    if "interpretation_diff" in report and not isinstance(report["interpretation_diff"], dict):
        raise SkillPublishGateError(message)
    if "accepted_warnings" in report:
        accepted = report["accepted_warnings"]
        if not isinstance(accepted, list) or any(not isinstance(code, str) for code in accepted):
            raise SkillPublishGateError(message)
    raw = report.get("findings")
    if not isinstance(raw, list):
        raise SkillPublishGateError(message)
    saved: list[ManifestGateFinding] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("code"), str)
            or not item["code"].strip()
            or item.get("severity") not in ("error", "warning", "info")
            or not isinstance(item.get("message"), str)
            or (item.get("path") is not None and not isinstance(item["path"], str))
        ):
            raise SkillPublishGateError(message)
        saved.append(
            ManifestGateFinding(
                code=item["code"],
                severity=item["severity"],
                message=item["message"],
                path=item.get("path"),
            )
        )
    passed, current = evaluation
    # 新検査で古い error/warning を消さず、保存済みの受理履歴も今回の権限にしない。
    findings = tuple(dict.fromkeys((*saved, *current)))
    errors = any(item.severity == "error" for item in findings)
    warnings = {item.code for item in findings if item.severity == "warning"}
    if not report["passed"] or not passed or errors or not warnings.issubset(accepted_warnings):
        raise SkillPublishGateError(message)
    return findings


class SkillVersionTransitionError(ValueError):
    """SkillVersion の不可変 lifecycle に反する状態遷移が要求されたことを表す。"""


class SkillVersionDeleteBlockedError(ValueError):
    """SkillVersion を物理削除できない参照が残っていることを表す。

    Run snapshot と ChangeProposal は frozen Manifest を指す監査の正本であり、版を消すと
    過去の実行が何を根拠に動いたか説明できなくなる。廃止済みでも参照が残る版は消させない。
    """


class PublishedTaskNotFoundError(LookupError):
    """指定 Project から実行可能な PUBLISHED task が見つからないことを表す。"""


class TaskInputInvalidError(ValueError):
    """Run 入力が task の input schema に適合しないことを表す。"""


@dataclass(frozen=True, slots=True)
class StoredProjectSkillVersion:
    """Project が明示的に有効化した精確 SkillVersion の監査 read model。"""

    project_id: UUID
    organization_id: UUID
    skill_version: StoredSkillVersion
    enabled_by: UUID
    enabled_at: datetime
    disabled_at: datetime | None
