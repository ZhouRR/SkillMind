"""PostgreSQL 上の SkillSource と SkillInterpretation 永続化を実装する。"""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.redaction import contains_sensitive_content
from skillmind.db.models import (
    ChangeProposal,
    FrontendModuleVersion,
    Project,
    ProjectSkillVersion,
    RunSkillSnapshot,
    RuntimeManifest,
    Skill,
    SkillCompositionItem,
    SkillInterpretation,
    SkillSource,
    SkillVersion,
    TaskSchedule,
    TaskScheduleOccurrence,
)
from skillmind.skills.design_validation import SkillDesignInvalidError, SkillDesignSource
from skillmind.skills.domain import (
    CreateSkillVersionDraftCommand,
    InlineSkillFile,
    ManifestGateFinding,
    PublishedTaskNotFoundError,
    SaveModelInterpretationCommand,
    SaveSkillPreviewCommand,
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillInterpretationStatus,
    SkillPreview,
    SkillPublishGateError,
    SkillSourceNotFoundError,
    SkillVersionDeleteBlockedError,
    SkillVersionEnablementConflictError,
    SkillVersionEnablementNotFoundError,
    SkillVersionNotFoundError,
    SkillVersionStatus,
    SkillVersionTransitionError,
    StoredInterpretationExecution,
    StoredProjectSkillVersion,
    StoredSkillPreview,
    StoredSkillSource,
    StoredSkillVersion,
    validate_skill_publication,
)
from skillmind.skills.task_catalog import PublishedTaskDescriptor, project_published_tasks
from skillmind.skills.task_flow_preview import TaskFlowPreviewInvalidError, TaskFlowPreviewSource

_VALIDATION_LOCATION = re.compile(
    r"(?P<path>/(?:[A-Za-z0-9_.-]{1,64}/)*[A-Za-z0-9_.-]{0,64}): "
    r"(?P<code>[A-Za-z][A-Za-z0-9_]{0,63})(?: .*)?"
)
# 公開契約の構造 field だけを許可する。candidate が作る map key は文字種だけで信用しない。
_VALIDATION_PATH_FIELDS = frozenset({
    "response_version", "source_hash", "interpreter", "skill_key", "version", "prompt_checksum",
    "normalized_package", "runtime_manifest_draft", "report", "capability_blueprint", "identity",
    "compatibility", "tasks", "capabilities", "tools", "resource_requirements", "guidance",
    "required_rules", "recommended_steps", "quality_criteria", "prohibited_actions", "assumptions",
    "questions", "diagnostics", "source_traces", "effect_intents", "interaction_points",
    "input_contract", "output_contract", "parameter_contract", "result_contract", "fields",
    "items", "key", "type", "required", "enum", "description", "contract_version", "minLength",
    "maxLength", "min_length", "max_length", "minimum", "maximum", "pattern", "capability",
    "resource_keys", "resource_key", "operation", "risk", "contract_source_trace", "workflows",
    "steps",
    "document_prerequisites", "mode", "approval_mode", "access", "kind", "source", "target",
    "path", "line_start", "line_end", "file_path", "field_path", "reference", "title", "summary",
    "confidence", "level", "name", "provider", "providers", "request_schema", "response_schema",
    "error_schema", "input_schema", "output_schema", "input_schema_checksum",
    "output_schema_checksum",
    "workflow", "view", "default_view", "permissions", "execution", "ui", "extensions",
})
_VALIDATION_ARRAY_FIELDS = frozenset({
    "tasks", "capabilities", "tools", "resource_requirements", "required_rules",
    "recommended_steps",
    "quality_criteria", "prohibited_actions", "assumptions", "questions", "diagnostics",
    "source_traces", "effect_intents", "interaction_points", "fields", "enum", "resource_keys",
    "document_prerequisites", "providers", "workflows", "steps",
})
_CONTRACT_VALIDATION_CODES = frozenset({
    "contract_constraint_invalid", "contract_constraint_out_of_range", "contract_depth_exceeded",
    "contract_description_invalid", "contract_description_too_long", "contract_enum_duplicate",
    "contract_enum_invalid", "contract_enum_limit_exceeded", "contract_enum_type_mismatch",
    "contract_field_duplicate", "contract_field_invalid", "contract_field_key_invalid",
    "contract_field_limit_exceeded", "contract_fields_invalid", "contract_items_missing",
    "contract_keyword_inapplicable", "contract_keyword_unknown", "contract_number_range_invalid",
    "contract_pattern_invalid", "contract_pattern_unsafe", "contract_required_invalid",
    "contract_string_range_invalid", "contract_type_invalid", "contract_version_invalid",
})
# 汎用 ValueError の本文は instance を含み得る。値を埋め込まない既知 message だけを許可する。
_STATIC_VALIDATION_DIAGNOSTICS = frozenset({
    "RuntimeManifest tasks must be an array",
    "Model RuntimeManifest tasks must define input_contract",
    "Model RuntimeManifest output_contract must be an object",
    "Task capability is not declared in capabilities",
    "Task resource key is not declared in resource_requirements",
    "Effect resource key is not declared in resource_requirements",
    "An apply effect intent must reference a resource requirement",
    "An apply effect intent must reference a write resource requirement",
    "An apply effect intent must keep the ask approval mode",
    "A required rule must cite a source trace",
    "Document prerequisites require source evidence",
    "Task contract draft must be an object",
    "candidate_generation:empty_response",
    "candidate_generation:invalid_json",
    "candidate_generation:truncated_output",
})


class SkillRepository:
    """一つの transaction 内で不変 source と interpretation を操作する。"""

    def __init__(self, session: AsyncSession) -> None:
        """Transaction-scoped database session を保持する。"""

        self._session = session

    async def save_preview(
        self, command: SaveSkillPreviewCommand, *, authorize: Callable[[], datetime]
    ) -> StoredSkillPreview:
        """同一 source/checksum を再利用し、deterministic preview を追加式に保存する。"""

        authorize()
        source = await self._find_source(command.organization_id, command.source_hash)
        now = authorize()
        if source is None:
            source_id = uuid4()
            source = SkillSource(
                id=source_id,
                organization_id=command.organization_id,
                name=command.name,
                source_type=command.source_type,
                storage_uri=command.storage_uri or f"database://skill-sources/{source_id}",
                content_hash=command.source_hash,
                source_version=None,
                imported_by=command.imported_by,
                source_snapshot_json=[
                    {"path": file.path, "content": file.content} for file in command.source_files
                ],
                source_file_index_json=[dict(item) for item in command.source_file_index],
                created_at=now,
            )
            self._session.add(source)
            # Source と解釈に relationship がないため、親の保存順序を autoflush に委ねない。
            await self._session.flush()
            authorize()

        interpretation = await self._find_interpretation(
            source.id,
            command.interpreter_version,
            command.checksum,
        )
        now = authorize()
        if interpretation is None:
            interpretation = SkillInterpretation(
                id=uuid4(),
                skill_source_id=source.id,
                origin="deterministic_parser",
                interpreter_version=command.interpreter_version,
                model=None,
                compatibility_level=command.compatibility_level,
                status=SkillInterpretationStatus.PREVIEW_READY.value,
                summary=f"Deterministic assisted preview for {command.name}",
                confidence=command.confidence,
                assumptions_json=[],
                questions_json=[],
                diagnostics_json=list(command.diagnostics),
                normalized_package_json=command.preview.normalized_package,
                manifest_draft_json=command.preview.runtime_manifest_draft,
                checksum=command.checksum,
                created_at=now,
            )
            self._session.add(interpretation)
        authorize()
        return self._to_stored(source, interpretation)

    async def source_exists(self, *, organization_id: UUID, source_hash: str) -> bool:
        """組織 gate の下で原 source の有無だけを調べ、再 upload による上書きを避ける。"""

        return await self._find_source(organization_id, source_hash) is not None

    async def get_source(
        self, *, organization_id: UUID, skill_source_id: UUID
    ) -> StoredSkillSource:
        """Interpret 実行の入力として Organization 所有 SkillSource snapshot を取得する。"""

        source = await self._session.get(SkillSource, skill_source_id)
        if source is None or source.organization_id != organization_id:
            raise SkillSourceNotFoundError(f"SkillSource not found: {skill_source_id}")
        return StoredSkillSource(
            skill_source_id=source.id,
            organization_id=source.organization_id,
            name=source.name,
            source_type=source.source_type,
            source_hash=source.content_hash,
            source_files=tuple(
                InlineSkillFile(path=str(item["path"]), content=str(item["content"]))
                for item in source.source_snapshot_json
                if isinstance(item, dict) and "path" in item and "content" in item
            ),
            storage_uri=source.storage_uri,
            source_file_index=tuple(
                dict(item)
                for item in (getattr(source, "source_file_index_json", None) or [])
                if isinstance(item, dict)
            ),
        )

    async def find_model_interpretation(
        self,
        *,
        organization_id: UUID,
        skill_source_id: UUID,
        interpreter_version: str,
        execution_key: str,
    ) -> StoredInterpretationExecution | None:
        """同一 identity の既存 model interpretation を idempotent に検索する。"""

        source = await self._session.get(SkillSource, skill_source_id)
        if source is None or source.organization_id != organization_id:
            raise SkillSourceNotFoundError(f"SkillSource not found: {skill_source_id}")
        existing = await self._find_model_row(source.id, interpreter_version, execution_key)
        if existing is None:
            return None
        return self._to_stored_execution(source, existing, reused=True)

    async def save_model_interpretation(
        self, command: SaveModelInterpretationCommand
    ) -> StoredInterpretationExecution:
        """成功/失敗を問わず、同一 execution key を idempotent に再利用して保存する。"""

        source = await self._session.get(SkillSource, command.skill_source_id)
        if source is None or source.organization_id != command.organization_id:
            raise SkillSourceNotFoundError(f"SkillSource not found: {command.skill_source_id}")
        existing = await self._find_model_row(
            source.id, command.interpreter_version, command.execution_key
        )
        if existing is not None:
            return self._to_stored_execution(source, existing, reused=True)
        interpretation = SkillInterpretation(
            id=uuid4(),
            skill_source_id=source.id,
            origin="model",
            interpreter_version=command.interpreter_version,
            model=command.model,
            compatibility_level=command.compatibility_level,
            status=command.status.value,
            summary=command.summary,
            confidence=command.confidence,
            assumptions_json=list(command.assumptions),
            questions_json=list(command.questions),
            diagnostics_json=list(command.diagnostics),
            normalized_package_json=command.normalized_package,
            manifest_draft_json=command.manifest_draft,
            report_json=command.report,
            execution_json=command.execution,
            parent_interpretation_id=command.parent_interpretation_id,
            adjustment_json=command.adjustment,
            # Idempotency は execution key を checksum 列に固定して uq 制約で保証する。
            checksum=command.execution_key,
            created_at=datetime.now(UTC),
        )
        self._session.add(interpretation)
        return self._to_stored_execution(source, interpretation, reused=False)

    async def find_terminal_by_execution_key(
        self, *, organization_id: UUID, execution_key: str
    ) -> tuple[UUID, str, str | None] | None:
        """SSE 再接続用に、execution key の確定済み record を軽量 tuple で返す。

        戻り値は (interpretation_id, status, error_code)。未確定(未実行)は None。
        """

        statement = (
            select(
                SkillInterpretation.id,
                SkillInterpretation.status,
                SkillInterpretation.execution_json,
            )
            .join(SkillSource, SkillInterpretation.skill_source_id == SkillSource.id)
            .where(
                SkillInterpretation.checksum == execution_key,
                SkillInterpretation.origin == "model",
                SkillSource.organization_id == organization_id,
            )
        )
        row = (await self._session.execute(statement)).first()
        if row is None:
            return None
        interpretation_id, status, execution = row
        error_code = None
        if isinstance(execution, dict):
            value = execution.get("error_code")
            error_code = value if isinstance(value, str) else None
        return interpretation_id, status, error_code

    async def get_model_interpretation(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredInterpretationExecution:
        """Organization 所有を確認して model interpretation の実行 detail を取得する。"""

        interpretation = await self._session.get(SkillInterpretation, interpretation_id)
        if interpretation is None:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        source = await self._session.get(SkillSource, interpretation.skill_source_id)
        if source is None or source.organization_id != organization_id:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        return self._to_stored_execution(source, interpretation, reused=False)

    async def get_interpretation(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredSkillPreview:
        """Organization 所有の interpretation と親 source を取得する。"""

        interpretation = await self._session.get(SkillInterpretation, interpretation_id)
        if interpretation is None:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        source = await self._session.get(SkillSource, interpretation.skill_source_id)
        if source is None or source.organization_id != organization_id:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        return self._to_stored(source, interpretation)

    async def create_version_draft(
        self,
        command: CreateSkillVersionDraftCommand,
        *,
        authorize: Callable[[], datetime],
    ) -> StoredSkillVersion:
        """Organization 所有 Interpretation から idempotent な frozen DRAFT を作成する。"""

        interpretation = await self._session.get(SkillInterpretation, command.interpretation_id)
        if interpretation is None:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {command.interpretation_id}"
            )
        source = await self._session.get(SkillSource, interpretation.skill_source_id)
        if source is None or source.organization_id != command.organization_id:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {command.interpretation_id}"
            )
        existing = (
            await self._session.scalars(
                select(SkillVersion).where(
                    SkillVersion.interpretation_id == command.interpretation_id
                )
            )
        ).one_or_none()
        if existing is not None:
            skill = await self._session.get(Skill, existing.skill_id)
            if skill is None or skill.organization_id != command.organization_id:
                raise SkillInterpretationNotFoundError("SkillInterpretation was not found")
            try:
                manifest = await self._required_manifest(existing.id)
            except RuntimeError:
                raise SkillInterpretationNotReadyError(
                    "Saved SkillVersion does not match its interpretation"
                ) from None
            if (
                existing.interpretation_id != interpretation.id
                or existing.skill_source_id != source.id
                or manifest.skill_version_id != existing.id
                or manifest.interpretation_id != interpretation.id
            ):
                raise SkillInterpretationNotReadyError(
                    "Saved SkillVersion does not match its interpretation"
                )
            authorize()
            return self._to_stored_version(skill, existing, manifest)

        # 失敗や中間状態の interpretation から発行可能な DRAFT を生成させない。
        status = SkillInterpretationStatus(interpretation.status)
        if status is not SkillInterpretationStatus.PREVIEW_READY:
            raise SkillInterpretationNotReadyError(
                f"SkillInterpretation is not preview-ready: {command.interpretation_id}"
            )
        identity = command.manifest.get("identity")
        if not isinstance(identity, dict) or not isinstance(identity.get("skill_key"), str):
            raise ValueError("Frozen Manifest has no skill key")
        skill_key = identity["skill_key"]
        existing_skill = (
            await self._session.scalars(
                select(Skill).where(
                    Skill.organization_id == command.organization_id,
                    Skill.key == skill_key,
                )
            )
        ).one_or_none()
        now = authorize()
        if existing_skill is None:
            metadata = interpretation.normalized_package_json.get("metadata", {})
            skill = Skill(
                id=uuid4(),
                organization_id=command.organization_id,
                key=skill_key,
                name=source.name,
                description=str(metadata.get("description", ""))
                if isinstance(metadata, dict)
                else "",
                status="DRAFT",
                created_at=now,
                updated_at=now,
            )
            self._session.add(skill)
            # SkillVersion の FK 親を先に確定する。relationship のない UOW の順序に頼らない。
            await self._session.flush()
        else:
            skill = existing_skill
        versions = list(
            (
                await self._session.scalars(
                    select(SkillVersion.version).where(SkillVersion.skill_id == skill.id)
                )
            ).all()
        )
        now = authorize()
        version_id = uuid4()
        version = SkillVersion(
            id=version_id,
            skill_id=skill.id,
            version=_next_patch_version(versions),
            skill_source_id=source.id,
            interpretation_id=interpretation.id,
            status=SkillVersionStatus.DRAFT.value,
            gate_report_json={
                "passed": command.gate_passed,
                "findings": [_finding_json(item) for item in command.gate_findings],
                "interpretation_diff": command.interpretation_diff,
                "accepted_warnings": [],
            },
            published_by=None,
            published_at=None,
            created_at=now,
            updated_at=now,
        )
        manifest = RuntimeManifest(
            id=uuid4(),
            interpretation_id=interpretation.id,
            skill_version_id=version_id,
            manifest_version=str(command.manifest["manifest_version"]),
            manifest_json=command.manifest,
            checksum=command.manifest_checksum,
            created_at=now,
        )
        self._session.add(version)
        # RuntimeManifest.skill_version_id は version.id への FK だが、両 model 間に
        # relationship がないため UOW は挿入順を依存関係から決められない。version と新規 skill を
        # 先に flush して親行を確定し、manifest 挿入時の FK 違反を環境非依存に防ぐ。
        await self._session.flush()
        authorize()
        self._session.add(manifest)
        return self._to_stored_version(skill, version, manifest)

    async def get_draft_design_source(
        self,
        *,
        organization_id: UUID,
        interpretation_id: UUID,
        manifest: dict[str, Any],
        manifest_checksum: str,
    ) -> SkillDesignSource:
        """初回凍結用に元の解釈と source を読み、文字列化や欠損の除去をしない。"""

        interpretation = await self._session.get(
            SkillInterpretation, interpretation_id, populate_existing=True
        )
        if interpretation is None:
            raise SkillInterpretationNotFoundError("SkillInterpretation was not found")
        source = await self._session.get(
            SkillSource, interpretation.skill_source_id, populate_existing=True
        )
        if source is None or source.organization_id != organization_id:
            raise SkillInterpretationNotFoundError("SkillInterpretation was not found")
        if interpretation.status != SkillInterpretationStatus.PREVIEW_READY.value or (
            manifest.get("capability_blueprint") is not None and interpretation.origin != "model"
        ):
            raise SkillInterpretationNotReadyError("SkillInterpretation is not preview-ready")
        identity = manifest.get("identity")
        skill_key = identity.get("skill_key") if isinstance(identity, dict) else None
        if not isinstance(skill_key, str):
            raise SkillInterpretationNotReadyError("Saved interpretation has no draft identity")
        return _design_source(source, interpretation, skill_key, manifest, manifest_checksum)

    async def get_skill_version(
        self, *, organization_id: UUID, skill_version_id: UUID
    ) -> StoredSkillVersion:
        """Source の Organization ownership を確認して frozen version を返す。"""

        skill, version, manifest = await self._version_models(organization_id, skill_version_id)
        return self._to_stored_version(skill, version, manifest)

    async def publish_skill_version(
        self,
        *,
        organization_id: UUID,
        skill_version_id: UUID,
        published_by: UUID,
        accepted_warnings: frozenset[str],
        validate: Callable[[SkillDesignSource], tuple[bool, tuple[ManifestGateFinding, ...]]],
        authorize: Callable[[], datetime],
    ) -> StoredSkillVersion:
        """全 hard gate と warning acceptance を強制して DRAFT を publish する。"""

        skill, version, manifest = await self._version_models(
            organization_id,
            skill_version_id,
            lock=True,
            missing_manifest_error=SkillPublishGateError(
                "SkillVersion publish gate has unresolved findings"
            ),
        )
        # 原資格の lock は service が先に取得する。版待機後の重放も現在の ADMIN を要する。
        authorize()
        if SkillVersionStatus(version.status) is SkillVersionStatus.PUBLISHED:
            return self._to_stored_version(skill, version, manifest)
        if SkillVersionStatus(version.status) is not SkillVersionStatus.DRAFT:
            raise SkillVersionTransitionError("Only a draft SkillVersion can be published")
        try:
            source = await self._version_design_source(organization_id, skill, version, manifest)
        except SkillDesignInvalidError:
            raise SkillPublishGateError(
                "SkillVersion publish gate has unresolved findings"
            ) from None
        findings = validate_skill_publication(
            report=version.gate_report_json,
            evaluation=validate(source),
            accepted_warnings=accepted_warnings,
        )
        # 原 source の読取と全 gate の検査時間も含め、metadata 変更直前に期限を判定する。
        now = authorize()
        version.status = SkillVersionStatus.PUBLISHED.value
        version.published_by = published_by
        version.published_at = now
        version.updated_at = now
        version.gate_report_json = {
            **version.gate_report_json,
            "passed": True,
            "findings": [_finding_json(item) for item in findings],
            "accepted_warnings": sorted(accepted_warnings),
        }
        skill.status = "PUBLISHED"
        skill.updated_at = now
        return self._to_stored_version(skill, version, manifest)

    async def deprecate_skill_version(
        self,
        *,
        organization_id: UUID,
        skill_version_id: UUID,
        authorize: Callable[[], datetime],
    ) -> StoredSkillVersion:
        """PUBLISHED 版を DEPRECATED へ進め、新規 Project 利用だけを閉じる。"""

        skill, version, manifest = await self._version_models(
            organization_id,
            skill_version_id,
            lock=True,
            missing_manifest_error=SkillVersionTransitionError(
                "SkillVersion has no frozen RuntimeManifest"
            ),
        )
        now = authorize()
        status = SkillVersionStatus(version.status)
        if status is SkillVersionStatus.DEPRECATED:
            return self._to_stored_version(skill, version, manifest)
        if status is not SkillVersionStatus.PUBLISHED:
            raise SkillVersionTransitionError("Only a published SkillVersion can be deprecated")
        version.status = SkillVersionStatus.DEPRECATED.value
        version.updated_at = now
        return self._to_stored_version(skill, version, manifest)

    async def delete_skill_version(
        self,
        *,
        organization_id: UUID,
        skill_version_id: UUID,
        authorize: Callable[[], datetime],
    ) -> None:
        """監査参照のない DRAFT / DEPRECATED 版と、その付随 row を物理削除する。

        Run snapshot/Proposal、Composition、Schedule/occurrence、生成 module は精確版を
        指す実行・構成の正本なので、状態を問わず一件でも参照があれば削除しない。

        逆に ProjectSkillVersion は「その Project から見えるか」の可視性設定にすぎず、参照先の
        版が消えれば意味を失うため一緒に削除する。RuntimeManifest は版と 1:1。
        """

        _, version, manifest = await self._version_models(
            organization_id,
            skill_version_id,
            lock=True,
            missing_manifest_error=SkillVersionDeleteBlockedError(
                "SkillVersion has no frozen RuntimeManifest"
            ),
        )
        authorize()
        if SkillVersionStatus(version.status) not in {
            SkillVersionStatus.DRAFT, SkillVersionStatus.DEPRECATED,
        }:
            raise SkillVersionDeleteBlockedError(
                "Only a draft or deprecated SkillVersion can be deleted"
            )
        for model in (
            RunSkillSnapshot,
            ChangeProposal,
            SkillCompositionItem,
            TaskSchedule,
            TaskScheduleOccurrence,
            FrontendModuleVersion,
        ):
            referenced = await self._session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.skill_version_id == skill_version_id)
            )
            authorize()
            if referenced:
                raise SkillVersionDeleteBlockedError(
                    "SkillVersion is still referenced by execution or configuration records"
                )
        await self._session.execute(
            delete(ProjectSkillVersion).where(
                ProjectSkillVersion.skill_version_id == skill_version_id
            )
        )
        authorize()
        await self._session.delete(manifest)
        authorize()
        await self._session.delete(version)

    async def list_skill_versions(self, *, organization_id: UUID) -> tuple[StoredSkillVersion, ...]:
        """Organization の frozen SkillVersion を identity/version 順で列挙する。"""

        statement = (
            select(Skill, SkillVersion, RuntimeManifest)
            .select_from(SkillVersion)
            .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .join(RuntimeManifest, RuntimeManifest.skill_version_id == SkillVersion.id)
            .where(
                SkillSource.organization_id == organization_id,
                Skill.organization_id == organization_id,
            )
            .order_by(Skill.key, SkillVersion.created_at, SkillVersion.id)
        )
        return tuple(
            self._to_stored_version(skill, version, manifest)
            for skill, version, manifest in (await self._session.execute(statement)).all()
        )

    async def enable_project_skill_version(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        enabled_by: UUID,
        authorize: Callable[[], datetime],
    ) -> StoredProjectSkillVersion:
        """同一 Organization の PUBLISHED 精確版を Project へ明示的に有効化する。"""

        await self._require_project_organization(project_id, organization_id)
        skill, version, manifest = await self._version_models(
            organization_id,
            skill_version_id,
            lock=True,
            missing_manifest_error=SkillVersionEnablementConflictError(
                "SkillVersion has no frozen RuntimeManifest"
            ),
        )
        authorize()
        if SkillVersionStatus(version.status) is not SkillVersionStatus.PUBLISHED:
            raise SkillVersionEnablementConflictError(
                "Only a published SkillVersion can be enabled"
            )
        existing = (
            await self._session.scalars(
                select(ProjectSkillVersion)
                .where(
                    ProjectSkillVersion.project_id == project_id,
                    ProjectSkillVersion.skill_version_id == skill_version_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        now = authorize()
        if existing is not None:
            if existing.disabled_at is not None:
                # disabled_at を消すと停用監査が失われる。再有効化 lifecycle を導入するまでは
                # 同じ関係を復活させず、新版の明示有効化を要求して履歴を保全する。
                raise SkillVersionEnablementConflictError(
                    "A disabled SkillVersion enablement cannot be reactivated"
                )
            return self._to_project_skill_version(
                organization_id, existing, skill, version, manifest
            )
        binding = ProjectSkillVersion(
            id=uuid4(),
            project_id=project_id,
            skill_version_id=skill_version_id,
            enabled_by=enabled_by,
            enabled_at=now,
            disabled_at=None,
        )
        self._session.add(binding)
        return self._to_project_skill_version(organization_id, binding, skill, version, manifest)

    async def disable_project_skill_version(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        authorize: Callable[[], datetime],
    ) -> StoredProjectSkillVersion:
        """Project の有効化を監査行ごと残したまま停用する。"""

        await self._require_project_organization(project_id, organization_id)
        skill, version, manifest = await self._version_models(
            organization_id,
            skill_version_id,
            lock=True,
            missing_manifest_error=SkillVersionEnablementConflictError(
                "SkillVersion has no frozen RuntimeManifest"
            ),
        )
        authorize()
        binding = (
            await self._session.scalars(
                select(ProjectSkillVersion)
                .where(
                    ProjectSkillVersion.project_id == project_id,
                    ProjectSkillVersion.skill_version_id == skill_version_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        now = authorize()
        if binding is None:
            raise SkillVersionEnablementNotFoundError(
                f"SkillVersion enablement not found: {skill_version_id}"
            )
        if binding.disabled_at is None:
            binding.disabled_at = now
        return self._to_project_skill_version(organization_id, binding, skill, version, manifest)

    async def list_project_skill_versions(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        include_disabled: bool = False,
    ) -> tuple[StoredProjectSkillVersion, ...]:
        """Project の有効化関係を version detail と共に列挙する。"""

        await self._require_project_organization(project_id, organization_id)
        statement = (
            select(ProjectSkillVersion, Skill, SkillVersion, RuntimeManifest)
            .select_from(ProjectSkillVersion)
            .join(SkillVersion, SkillVersion.id == ProjectSkillVersion.skill_version_id)
            .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .join(RuntimeManifest, RuntimeManifest.skill_version_id == SkillVersion.id)
            .where(
                ProjectSkillVersion.project_id == project_id,
                SkillSource.organization_id == organization_id,
                Skill.organization_id == organization_id,
            )
            .order_by(Skill.key, SkillVersion.created_at, SkillVersion.id)
        )
        if not include_disabled:
            statement = statement.where(ProjectSkillVersion.disabled_at.is_(None))
        return tuple(
            self._to_project_skill_version(organization_id, binding, skill, version, manifest)
            for binding, skill, version, manifest in (await self._session.execute(statement)).all()
        )

    async def list_published_task_descriptors(
        self, *, project_id: UUID
    ) -> tuple[PublishedTaskDescriptor, ...]:
        """Project が明示的に有効化した PUBLISHED SkillVersion から task を投影する。

        ProjectSkillVersion の active 行だけを可視性の正本とし、最新版の解決は行わない。
        停用済み・未発行版は既存 Run の監査には残るが、新規 TaskCatalog へは出さない。
        """

        statement = (
            select(Skill, SkillVersion, RuntimeManifest)
            .select_from(SkillVersion)
            .join(
                ProjectSkillVersion,
                ProjectSkillVersion.skill_version_id == SkillVersion.id,
            )
            .join(Project, Project.id == ProjectSkillVersion.project_id)
            .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .join(RuntimeManifest, RuntimeManifest.skill_version_id == SkillVersion.id)
            .where(
                ProjectSkillVersion.project_id == project_id,
                ProjectSkillVersion.disabled_at.is_(None),
                SkillVersion.status == SkillVersionStatus.PUBLISHED.value,
                Project.organization_id == SkillSource.organization_id,
                Skill.organization_id == SkillSource.organization_id,
            )
            .order_by(Skill.key, SkillVersion.version)
        )
        rows = (await self._session.execute(statement)).all()
        descriptors: list[PublishedTaskDescriptor] = []
        for skill, version, manifest in rows:
            descriptors.extend(
                project_published_tasks(
                    skill_id=skill.id,
                    skill_version_id=version.id,
                    skill_key=skill.key,
                    skill_name=skill.name,
                    version=version.version,
                    published_at=version.published_at,
                    manifest=manifest.manifest_json,
                )
            )
        return tuple(descriptors)

    async def require_current_task_binding(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        authorize: Callable[[], datetime] | None = None,
    ) -> None:
        """新規 Run/調度/組合で、精確版の現行可用性を保存 transaction に固定する。

        呼出元は現在の資格/Project を先に固定する。SkillVersion → 有効化の順を二つの
        SELECT で保証し、Schedule 認領が必要とする外鍵 KEY SHARE と互換な SHARE を使う。
        原 Run の重放では呼ばず、Manifest の再生成や resource/Provider 解決も行わない。
        原会話の再検証 callback があれば各 row 待機直後、対象拒否より先に呼ぶ。
        """

        version = await self._session.scalar(
            select(SkillVersion)
            .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .join(Project, Project.organization_id == Skill.organization_id)
            .where(
                SkillVersion.id == skill_version_id,
                SkillVersion.status == SkillVersionStatus.PUBLISHED.value,
                Skill.organization_id == organization_id,
                SkillSource.organization_id == organization_id,
                Project.id == project_id,
            )
            .with_for_update(read=True, of=SkillVersion)
            .execution_options(populate_existing=True)
        )
        if authorize is not None:
            authorize()
        if version is None:
            raise PublishedTaskNotFoundError("Published task is not available")
        binding = await self._session.scalar(
            select(ProjectSkillVersion)
            .where(
                ProjectSkillVersion.project_id == project_id,
                ProjectSkillVersion.skill_version_id == skill_version_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if authorize is not None:
            authorize()
        if binding is None or binding.disabled_at is not None:
            raise PublishedTaskNotFoundError("Published task is not available")

    async def get_published_task_binding(
        self, *, project_id: UUID, skill_version_id: UUID
    ) -> tuple[Skill, SkillVersion, RuntimeManifest]:
        """Project で active に有効化済みかつ PUBLISHED の aggregate を Run 解決用に取得する。

        DRAFT/DEPRECATED は実行不能なので、存在と越権と未公開を同じ 404 に畳み込む。
        """

        statement = (
            select(Skill, SkillVersion, RuntimeManifest)
            .select_from(ProjectSkillVersion)
            .join(SkillVersion, SkillVersion.id == ProjectSkillVersion.skill_version_id)
            .join(Project, Project.id == ProjectSkillVersion.project_id)
            .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .join(RuntimeManifest, RuntimeManifest.skill_version_id == SkillVersion.id)
            .where(
                ProjectSkillVersion.project_id == project_id,
                ProjectSkillVersion.skill_version_id == skill_version_id,
                ProjectSkillVersion.disabled_at.is_(None),
                SkillVersion.status == SkillVersionStatus.PUBLISHED.value,
                Project.organization_id == SkillSource.organization_id,
                Skill.organization_id == SkillSource.organization_id,
            )
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            raise SkillVersionNotFoundError(f"SkillVersion not found: {skill_version_id}")
        skill, version, manifest = row
        return skill, version, manifest

    async def get_task_flow_preview_source(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
    ) -> TaskFlowPreviewSource:
        """有効な精確版を認可してから、補正していない原 source JSON と解釈 identity を読む。"""

        skill, version, manifest = await self.get_published_task_binding(
            project_id=project_id,
            skill_version_id=skill_version_id,
        )
        if (
            skill.organization_id != organization_id
            or version.status != SkillVersionStatus.PUBLISHED.value
            or version.id != skill_version_id
        ):
            raise SkillVersionNotFoundError("Task Flow preview was not found")
        try:
            design = await self._version_design_source(organization_id, skill, version, manifest)
        except SkillDesignInvalidError:
            raise TaskFlowPreviewInvalidError() from None
        return TaskFlowPreviewSource(
            project_id=project_id,
            skill_id=skill.id,
            skill_version_id=version.id,
            skill_key=design.skill_key,
            version=version.version,
            manifest_checksum=design.manifest_checksum,
            manifest=design.manifest,
            skill_source_id=version.skill_source_id,
            source_hash=design.source_hash,
            source_file_index=design.source_file_index,
            source_snapshot=design.source_snapshot,
            interpretation_id=design.interpretation_id,
            interpreter_version=design.interpreter_version,
        )

    async def _version_design_source(
        self,
        organization_id: UUID,
        skill: Skill,
        version: SkillVersion,
        manifest: RuntimeManifest,
    ) -> SkillDesignSource:
        """発行とプレビューで同じ原 aggregate の FK と解釈由来を確認する。"""

        source = await self._session.get(
            SkillSource,
            version.skill_source_id,
            populate_existing=True,
        )
        if source is not None and source.organization_id != organization_id:
            raise SkillVersionNotFoundError("SkillVersion was not found")
        interpretation = await self._session.get(
            SkillInterpretation,
            version.interpretation_id,
            populate_existing=True,
        )
        if (
            source is None
            or interpretation is None
            or skill.id != version.skill_id
            or source.id != version.skill_source_id
            or interpretation.id != version.interpretation_id
            or interpretation.skill_source_id != source.id
            or interpretation.status != SkillInterpretationStatus.PREVIEW_READY.value
            or manifest.skill_version_id != version.id
            or manifest.interpretation_id != interpretation.id
            or not isinstance(manifest.manifest_json, dict)
            or manifest.manifest_version != manifest.manifest_json.get("manifest_version")
            or (
                manifest.manifest_json.get("capability_blueprint") is not None
                and interpretation.origin != "model"
            )
        ):
            raise SkillDesignInvalidError()
        # 現行 published aggregate は PREVIEW_READY を参照する。将来 SUPERSEDED を書く場合も
        # 新しい解釈だけを理由に、既発行版を暗黙に別 source へ差し替えてはならない。
        # 既存 get_source の str/filter 補正は使わず、破損や欠落を純投影の検査へ渡す。
        return _design_source(
            source, interpretation, skill.key, manifest.manifest_json, manifest.checksum
        )

    async def _version_models(
        self,
        organization_id: UUID,
        skill_version_id: UUID,
        *,
        lock: bool = False,
        missing_manifest_error: ValueError | None = None,
    ) -> tuple[Skill, SkillVersion, RuntimeManifest]:
        """Organization ownership を source と Skill identity の双方で検証する。"""

        version = await self._session.get(
            SkillVersion, skill_version_id, with_for_update=lock, populate_existing=lock
        )
        if version is None:
            raise SkillVersionNotFoundError(f"SkillVersion not found: {skill_version_id}")
        source = await self._session.get(
            SkillSource, version.skill_source_id, populate_existing=lock
        )
        if source is None or source.organization_id != organization_id:
            raise SkillVersionNotFoundError(f"SkillVersion not found: {skill_version_id}")
        skill = await self._session.get(Skill, version.skill_id, populate_existing=lock)
        if skill is None or skill.organization_id != organization_id:
            raise SkillVersionNotFoundError(f"SkillVersion not found: {skill_version_id}")
        try:
            manifest = await self._required_manifest(version.id)
        except RuntimeError:
            # 同じ版 lock を使う廃止/削除/啓停を、発行専用の拒否へ誤分類しない。
            if missing_manifest_error is not None:
                raise missing_manifest_error from None
            raise
        return skill, version, manifest

    async def _required_skill(self, skill_id: UUID) -> Skill:
        """Version foreign key が参照する Skill を取得する。"""

        skill = await self._session.get(Skill, skill_id)
        if skill is None:
            raise RuntimeError("SkillVersion references a missing Skill")
        return skill

    async def _required_manifest(self, skill_version_id: UUID) -> RuntimeManifest:
        """Version に一対一で固定された RuntimeManifest を取得する。"""

        manifest = (
            await self._session.scalars(
                select(RuntimeManifest)
                .where(RuntimeManifest.skill_version_id == skill_version_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if manifest is None:
            raise RuntimeError("SkillVersion references a missing RuntimeManifest")
        return manifest

    async def _require_project_organization(self, project_id: UUID, organization_id: UUID) -> None:
        """Project が actor と同じ Organization に属することを repository 境界でも保証する。"""

        statement = select(Project.id).where(
            Project.id == project_id,
            Project.organization_id == organization_id,
        )
        if (await self._session.scalars(statement)).one_or_none() is None:
            # Project の不存在と Organization 越権は上位 API で同じ 404 に畳むため、ここでも
            # version の可視性を公開しない同型 error にする。
            raise SkillVersionEnablementNotFoundError(
                f"Project SkillVersion scope not found: {project_id}"
            )

    async def _find_source(self, organization_id: UUID, content_hash: str) -> SkillSource | None:
        """Organization と content hash が一致する既存 source を検索する。"""

        statement = select(SkillSource).where(
            SkillSource.organization_id == organization_id,
            SkillSource.content_hash == content_hash,
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def _find_interpretation(
        self,
        source_id: UUID,
        interpreter_version: str,
        checksum: str,
    ) -> SkillInterpretation | None:
        """Source、parser version、checksum が一致する preview を検索する。"""

        statement = select(SkillInterpretation).where(
            SkillInterpretation.skill_source_id == source_id,
            SkillInterpretation.interpreter_version == interpreter_version,
            SkillInterpretation.checksum == checksum,
        )
        return (await self._session.scalars(statement)).one_or_none()

    async def _find_model_row(
        self, source_id: UUID, interpreter_version: str, execution_key: str
    ) -> SkillInterpretation | None:
        """Source、system Skill version、execution key が一致する model 実行を検索する。"""

        statement = select(SkillInterpretation).where(
            SkillInterpretation.skill_source_id == source_id,
            SkillInterpretation.interpreter_version == interpreter_version,
            SkillInterpretation.checksum == execution_key,
        )
        return (await self._session.scalars(statement)).one_or_none()

    @staticmethod
    def _to_stored_execution(
        source: SkillSource,
        interpretation: SkillInterpretation,
        *,
        reused: bool,
    ) -> StoredInterpretationExecution:
        """Model interpretation 行を API 非依存の実行結果 read model へ変換する。"""

        execution = (
            interpretation.execution_json if isinstance(interpretation.execution_json, dict) else {}
        )
        error_code = execution.get("error_code")
        return StoredInterpretationExecution(
            interpretation_id=interpretation.id,
            skill_source_id=source.id,
            organization_id=source.organization_id,
            status=SkillInterpretationStatus(interpretation.status),
            origin=interpretation.origin,
            model=interpretation.model,
            interpreter_version=interpretation.interpreter_version,
            execution_key=interpretation.checksum,
            error_code=str(error_code) if error_code is not None else None,
            compatibility_level=interpretation.compatibility_level,
            confidence=interpretation.confidence,
            summary=interpretation.summary,
            created_at=interpretation.created_at,
            preview=SkillPreview(
                normalized_package=interpretation.normalized_package_json,
                runtime_manifest_draft=interpretation.manifest_draft_json,
            ),
            report=interpretation.report_json,
            reused=reused,
            parent_interpretation_id=interpretation.parent_interpretation_id,
            adjustment=interpretation.adjustment_json,
            validation_attempts=_validation_attempts(execution),
        )

    @staticmethod
    def _to_stored(
        source: SkillSource,
        interpretation: SkillInterpretation,
    ) -> StoredSkillPreview:
        """Persistence model を API 非依存の read model へ変換する。"""

        return StoredSkillPreview(
            skill_source_id=source.id,
            interpretation_id=interpretation.id,
            organization_id=source.organization_id,
            name=source.name,
            source_hash=source.content_hash,
            source_type=source.source_type,
            interpretation_status=SkillInterpretationStatus(interpretation.status),
            compatibility_level=interpretation.compatibility_level,
            confidence=interpretation.confidence,
            interpreter_version=interpretation.interpreter_version,
            created_at=interpretation.created_at,
            preview=SkillPreview(
                normalized_package=interpretation.normalized_package_json,
                runtime_manifest_draft=interpretation.manifest_draft_json,
            ),
        )

    @staticmethod
    def _to_stored_version(
        skill: Skill, version: SkillVersion, manifest: RuntimeManifest
    ) -> StoredSkillVersion:
        """Frozen version aggregate を API 非依存 read model へ変換する。"""

        report = version.gate_report_json
        return StoredSkillVersion(
            skill_id=skill.id,
            skill_version_id=version.id,
            skill_source_id=version.skill_source_id,
            interpretation_id=version.interpretation_id,
            organization_id=skill.organization_id,
            skill_key=skill.key,
            name=skill.name,
            description=skill.description,
            version=version.version,
            status=SkillVersionStatus(version.status),
            manifest_checksum=manifest.checksum,
            manifest=manifest.manifest_json,
            gate_passed=bool(report.get("passed", False)),
            gate_findings=_findings_from_report(report),
            interpretation_diff=dict(report.get("interpretation_diff", {})),
            created_at=version.created_at,
            published_by=version.published_by,
            published_at=version.published_at,
        )

    @classmethod
    def _to_project_skill_version(
        cls,
        organization_id: UUID,
        binding: ProjectSkillVersion,
        skill: Skill,
        version: SkillVersion,
        manifest: RuntimeManifest,
    ) -> StoredProjectSkillVersion:
        """有効化行と frozen version aggregate を Project 管理用 read model へ変換する。"""

        return StoredProjectSkillVersion(
            project_id=binding.project_id,
            organization_id=organization_id,
            skill_version=cls._to_stored_version(skill, version, manifest),
            enabled_by=binding.enabled_by,
            enabled_at=binding.enabled_at,
            disabled_at=binding.disabled_at,
        )


def _design_source(
    source: SkillSource,
    interpretation: SkillInterpretation,
    skill_key: str,
    manifest: dict[str, Any],
    manifest_checksum: str,
) -> SkillDesignSource:
    """認可済み行の原 JSON を共通 validator へ渡し、正常化による破損隠しを防ぐ。"""

    return SkillDesignSource(
        skill_key=skill_key,
        manifest_checksum=manifest_checksum,
        manifest=deepcopy(manifest),
        source_hash=source.content_hash,
        source_file_index=deepcopy(source.source_file_index_json),
        source_snapshot=deepcopy(source.source_snapshot_json),
        interpretation_id=interpretation.id,
        interpreter_version=interpretation.interpreter_version,
    )


def _validation_attempts(execution: dict[str, Any]) -> tuple[str, ...]:
    """保存済みの脱敏診断だけを有界投影し、異常値や内部 execution 全体を公開しない。"""

    if "validation_attempts" in execution:
        values = execution["validation_attempts"]
    elif execution.get("error_code") == "schema_validation_failed" and "detail" in execution:
        # 旧 writer の単一診断を読み取るだけで、原 record の補記・上書きはしない。
        values = [execution["detail"]]
    else:
        return ()
    if not isinstance(values, list) or len(values) > 2:
        return ()
    if any(
        not isinstance(value, str) or not value.strip() or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or contains_sensitive_content(value)
        for value in values
    ):
        return ()
    projected = tuple(_public_validation_diagnostic(value) for value in values)
    if any(value is None for value in projected):
        return ()
    return tuple(value for value in projected if value is not None)


def _public_validation_diagnostic(value: str) -> str | None:
    """識別できる path/code だけを残し、enum/const/追加 key 等の自由な尾部を出さない。"""

    if value in _STATIC_VALIDATION_DIAGNOSTICS:
        return value
    match = _VALIDATION_LOCATION.fullmatch(value)
    if match is None:
        return None
    parts = match["path"].strip("/").split("/")
    for index, part in enumerate(parts):
        if not part or part in _VALIDATION_PATH_FIELDS:
            continue
        if not (
            part.isdecimal() and len(part) <= 5 and index > 0
            and parts[index - 1] in _VALIDATION_ARRAY_FIELDS
        ):
            return None
    code = match["code"]
    if code not in Draft202012Validator.VALIDATORS and code not in _CONTRACT_VALIDATION_CODES:
        return None
    return f"{match['path']}: {code}"


def _next_patch_version(versions: list[str]) -> str:
    """既存 semantic version の最大 patch に一を加え、初版は 0.1.0 とする。"""

    parsed: list[tuple[int, int, int]] = []
    for version in versions:
        parts = version.split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            parsed.append((int(parts[0]), int(parts[1]), int(parts[2])))
    if not parsed:
        return "0.1.0"
    major, minor, patch = max(parsed)
    return f"{major}.{minor}.{patch + 1}"


def _finding_json(finding: ManifestGateFinding) -> dict[str, object]:
    """Gate finding を JSON persistence shape へ変換する。"""

    return {
        "code": finding.code,
        "severity": finding.severity,
        "message": finding.message,
        "path": finding.path,
    }


def _findings_from_report(report: dict[str, object]) -> tuple[ManifestGateFinding, ...]:
    """Persistence report から型付き finding を復元する。"""

    raw = report.get("findings")
    if not isinstance(raw, list):
        return ()
    return tuple(
        ManifestGateFinding(
            code=str(item.get("code", "unknown")),
            severity=str(item.get("severity", "error")),
            message=str(item.get("message", "")),
            path=str(item["path"]) if item.get("path") is not None else None,
        )
        for item in raw
        if isinstance(item, dict)
    )
