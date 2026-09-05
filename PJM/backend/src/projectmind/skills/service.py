"""Skill の deterministic parse、保存、取得 use case を実装する。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.core.logging import log_event
from projectmind.skills.capability_blueprint import bind_blueprint_identity
from projectmind.skills.domain import (
    CreateSkillVersionDraftCommand,
    InlineSkillFile,
    InterpretationLaunch,
    PublishedTaskNotFoundError,
    SaveModelInterpretationCommand,
    SaveSkillPreviewCommand,
    SkillInterpretationNotReadyError,
    SkillInterpretationStatus,
    SkillInterpreterUnavailableError,
    SkillPreview,
    SkillSourceIntegrityError,
    SkillStorageUnavailableError,
    StoredInterpretationExecution,
    StoredProjectSkillVersion,
    StoredSkillPreview,
    StoredSkillSource,
    StoredSkillVersion,
    TaskInputInvalidError,
    UploadSkillFile,
)
from projectmind.skills.importer import (
    DeterministicManifestDraftBuilder,
    NormalizedSkillPackage,
    SkillImportError,
    SkillImportLimits,
    SkillPackageParser,
)
from projectmind.skills.interpretation_diff import diff_interpretations
from projectmind.skills.interpreter import (
    CapabilityCatalogSnapshot,
    InterpreterFixtureRunner,
    InterpreterSystemSkillIdentity,
    SkillStaticAnalysis,
    SkillStaticAnalyzer,
    UnsafeSkillSourceError,
    build_interpreter_request,
    load_inline_text_files,
)
from projectmind.skills.interpreter_execution import (
    InterpreterErrorCode,
    InterpreterExecutionError,
    InterpretProgressCallback,
    SkillInterpreter,
    candidate_repair_feedback,
    compute_execution_key,
)
from projectmind.skills.manifest_gate import ManifestValidator
from projectmind.skills.repository import SkillRepository
from projectmind.skills.resource_binding import (
    ProjectResourceCatalog,
    evaluate_blueprint_readiness,
)
from projectmind.skills.runtime_defaults import normalize_runtime_manifest
from projectmind.skills.task_catalog import (
    PublishedTaskDescriptor,
    ResolvedTaskRun,
    resolve_task_run_from_manifest,
)
from projectmind.skills.task_contract import TaskContractCompilationError
from projectmind.storage import FileStorage, FileStorageError, sanitize_object_key

logger = logging.getLogger(__name__)

# Model 候補の decode または platform validation が落ちた場合、一度だけ完全再生成する。
_MAX_CANDIDATE_REPAIR_ATTEMPTS = 1


class SkillService:
    """Deterministic parser と Skill persistence の transaction 境界を所有する。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        contracts_dir: Path,
        *,
        interpreter: SkillInterpreter | None = None,
        capability_catalog: CapabilityCatalogSnapshot | None = None,
        interpreter_identity: InterpreterSystemSkillIdentity | None = None,
        default_model: str | None = None,
        file_storage: FileStorage | None = None,
        storage_bucket: str = "projectmind",
        resource_catalog: ProjectResourceCatalog | None = None,
        registered_write_capabilities: frozenset[str] = frozenset(),
        installed_provider_capabilities: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        """Database factory、RuntimeManifest Schema、任意の model interpreter を保持する。"""

        self._session_factory = session_factory
        self._contracts_dir = contracts_dir.resolve()
        schema_path = self._contracts_dir / "runtime-manifest" / "v1alpha1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ValueError("RuntimeManifest Schema must be a JSON object")
        self._manifest_builder = DeterministicManifestDraftBuilder(schema)
        self._manifest_validator = ManifestValidator(contracts_dir)
        # 就緒度は publish gate と同じ Tool catalog を正本にする。二重管理すると「発行できるが
        # 永遠に CONFIGURATION_REQUIRED」のような食い違いが生まれる。
        self._resource_catalog = resource_catalog
        self._registered_capabilities = self._manifest_validator.registered_capabilities
        # ACTIONABLE は実 Provider registry と同じ write capability set だけで判定する。
        # Skill 文書や Integration 自己申告から write 権限を作らない。
        self._registered_write_capabilities = registered_write_capabilities
        # 就緒度は「catalog 登録」だけでなく「実装配線済み Provider」まで見る (計画 §19 W1)。
        # None のままなら Provider 検査を省く従来挙動 (offline/未配線環境) を保つ。
        self._installed_provider_capabilities = installed_provider_capabilities
        self._parser = SkillPackageParser(
            limits=SkillImportLimits(
                max_files=100,
                max_file_bytes=1_000_000,
                max_total_bytes=5_000_000,
                max_markdown_bytes=1_000_000,
            )
        )
        # interpret は import と分離した明示操作であり、未配線なら呼び出し時に閉じる。
        self._interpreter = interpreter
        self._capability_catalog = capability_catalog
        self._interpreter_identity = interpreter_identity
        self._default_model = default_model
        self._fixture_runner: InterpreterFixtureRunner | None = None
        # Upload import は binary bundle を object storage へ保存する。
        # 未配線のまま upload されたら実行時に閉じる。
        self._file_storage = file_storage
        self._storage_bucket = storage_bucket

    def preview_inline(self, files: Sequence[InlineSkillFile]) -> SkillPreview:
        """Inline text files を一時 directory で解析し、非永続 preview を返す。"""

        normalized_files = tuple(files)
        with TemporaryDirectory(prefix="projectmind-skill-") as temporary:
            source_root = Path(temporary).resolve()
            _write_inline_skill_files(normalized_files, source_root)
            package = self._parser.parse_directory(source_root)
            manifest = self._manifest_builder.build(package)
        return SkillPreview(
            normalized_package=package.to_dict(),
            runtime_manifest_draft=manifest,
        )

    async def save_inline(
        self,
        *,
        organization_id: UUID,
        imported_by: UUID,
        files: Sequence[InlineSkillFile],
    ) -> StoredSkillPreview:
        """Inline source と deterministic interpretation を同じ transaction で保存する。"""

        normalized_files = tuple(files)
        preview = self.preview_inline(normalized_files)
        command = _save_command(
            organization_id=organization_id,
            imported_by=imported_by,
            files=normalized_files,
            preview=preview,
        )
        async with self._session_factory() as session, session.begin():
            return await SkillRepository(session).save_preview(command)

    def preview_upload(self, files: Sequence[UploadSkillFile]) -> SkillPreview:
        """Multipart upload (binary 可) を一時 directory で解析し、非永続 preview を返す。"""

        normalized_files = _normalize_upload_skill_root(tuple(files))
        with TemporaryDirectory(prefix="projectmind-skill-") as temporary:
            source_root = Path(temporary).resolve()
            _write_upload_skill_files(normalized_files, source_root)
            package = self._parser.parse_directory(source_root)
            manifest = self._manifest_builder.build(package)
        return SkillPreview(
            normalized_package=package.to_dict(),
            runtime_manifest_draft=manifest,
        )

    async def save_upload(
        self,
        *,
        organization_id: UUID,
        imported_by: UUID,
        files: Sequence[UploadSkillFile],
    ) -> StoredSkillPreview:
        """Upload source を正規化し、binary bundle を object storage へ保存して永続化する。

        Inline 経路と同じ deterministic parser を通すため、正規化結果は inline と一致する。
        binary asset は text snapshot に載せられないため raw bundle を object storage に保存し、
        SkillSource.storage_uri を s3:// に固定する。text file は従来通り snapshot にも残す。
        """

        storage = self._require_storage()
        normalized_files = _normalize_upload_skill_root(tuple(files))
        with TemporaryDirectory(prefix="projectmind-skill-") as temporary:
            source_root = Path(temporary).resolve()
            _write_upload_skill_files(normalized_files, source_root)
            package = self._parser.parse_directory(source_root)
            manifest = self._manifest_builder.build(package)
            preview = SkillPreview(
                normalized_package=package.to_dict(),
                runtime_manifest_draft=manifest,
            )
            text_files = _upload_text_source_files(package, source_root)
            # content_hash 由来の key は同一内容の再 upload を冪等にし、id 生成順への依存を避ける。
            hash_segment = package.content_hash.replace(":", "-")
            key_prefix = f"organizations/{organization_id}/skill-sources/{hash_segment}"
            await _store_upload_bundle(storage, key_prefix, normalized_files)
        command = _save_command(
            organization_id=organization_id,
            imported_by=imported_by,
            files=text_files,
            preview=preview,
            storage_uri=f"s3://{self._storage_bucket}/{key_prefix}",
        )
        async with self._session_factory() as session, session.begin():
            return await SkillRepository(session).save_preview(command)

    def _require_storage(self) -> FileStorage:
        """Object storage が未配線なら upload を閉じて 503 相当の error を送出する。"""

        if self._file_storage is None:
            raise SkillStorageUnavailableError("Object storage is not configured for uploads")
        return self._file_storage

    async def get_interpretation(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredSkillPreview:
        """Organization 内の保存済み interpretation preview を取得する。"""

        async with self._session_factory() as session:
            return await SkillRepository(session).get_interpretation(
                organization_id=organization_id,
                interpretation_id=interpretation_id,
            )

    async def interpret(
        self,
        *,
        organization_id: UUID,
        skill_source_id: UUID,
        model: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        force_regenerate: bool = False,
        regeneration_nonce: str | None = None,
        on_event: InterpretProgressCallback | None = None,
    ) -> StoredInterpretationExecution:
        """保存済み source を model で解釈し、成功/失敗を idempotent に永続化する。

        Import で自動実行せず、明示的に呼ばれた時だけ model を起動する。危険 source は
        model 呼び出し前に UNSAFE_SOURCE として記録し、発行可能な DRAFT を作らない。
        """

        interpreter, catalog, identity = self._require_interpreter()
        resolved_model = self._resolve_model(model)
        resolved = dict(parameters or {})
        nonce = regeneration_nonce or (uuid4().hex if force_regenerate else None)
        async with self._session_factory() as session:
            source = await SkillRepository(session).get_source(
                organization_id=organization_id, skill_source_id=skill_source_id
            )
        return await self._run_interpretation(
            source=source,
            interpreter=interpreter,
            catalog=catalog,
            identity=identity,
            model=resolved_model,
            parameters=resolved,
            previous=None,
            adjustment=None,
            parent_id=None,
            regeneration_nonce=nonce,
            on_event=on_event,
        )

    async def begin_interpret(
        self,
        *,
        organization_id: UUID,
        skill_source_id: UUID,
        model: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        force_regenerate: bool = False,
    ) -> InterpretationLaunch:
        """Model を呼ばずに interpret 要求を受理する。

        再利用・unsafe 失敗は同期で確定 record を返し、それ以外は Worker job への
        引き渡し引数を返す。API 容器は model への egress を持たない配備が正であるため、
        model 呼び出し自体は必ず Worker 側の interpret() が行う。
        """

        _, catalog, identity = self._require_interpreter()
        resolved_model = self._resolve_model(model)
        resolved = dict(parameters or {})
        regeneration_nonce = uuid4().hex if force_regenerate else None
        async with self._session_factory() as session:
            source = await SkillRepository(session).get_source(
                organization_id=organization_id, skill_source_id=skill_source_id
            )
        stored, key, _prepared = await self._prepare_launch(
            source=source,
            catalog=catalog,
            identity=identity,
            model=resolved_model,
            parameters=resolved,
            previous=None,
            adjustment=None,
            parent_id=None,
            regeneration_nonce=regeneration_nonce,
        )
        if stored is not None:
            return InterpretationLaunch(
                status="stored", execution_key=key, stored=stored, job_name="", job_kwargs={}
            )
        return InterpretationLaunch(
            status="queued",
            execution_key=key,
            stored=None,
            job_name="interpret_skill_source_job",
            job_kwargs={
                "organization_id": str(organization_id),
                "skill_source_id": str(skill_source_id),
                "model": resolved_model,
                "parameters": resolved,
                "execution_key": key,
                "force_regenerate": force_regenerate,
                "regeneration_nonce": regeneration_nonce,
            },
        )

    async def adjust_interpretation(
        self,
        *,
        organization_id: UUID,
        interpretation_id: UUID,
        instruction: str,
        actor_id: UUID,
        model: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        on_event: InterpretProgressCallback | None = None,
    ) -> StoredInterpretationExecution:
        """親 interpretation に追加式 adjustment を適用し、新しい reinterpretation を作る。

        親は書き換えず、adjustment を request identity に折り込んで新しい実行を生成する。
        """

        interpreter, catalog, identity = self._require_interpreter()
        resolved_model = self._resolve_model(model)
        resolved = dict(parameters or {})
        parent, source, adjustment = await self._load_adjust_context(
            organization_id=organization_id,
            interpretation_id=interpretation_id,
            instruction=instruction,
            actor_id=actor_id,
        )
        return await self._run_interpretation(
            source=source,
            interpreter=interpreter,
            catalog=catalog,
            identity=identity,
            model=resolved_model,
            parameters=resolved,
            previous=self._previous_interpretation_summary(parent),
            adjustment=adjustment,
            parent_id=interpretation_id,
            regeneration_nonce=None,
            on_event=on_event,
        )

    async def begin_adjust(
        self,
        *,
        organization_id: UUID,
        interpretation_id: UUID,
        instruction: str,
        actor_id: UUID,
        model: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> InterpretationLaunch:
        """Model を呼ばずに adjust 要求を受理する。404/409 は同期のまま返す。"""

        _, catalog, identity = self._require_interpreter()
        resolved_model = self._resolve_model(model)
        resolved = dict(parameters or {})
        parent, source, adjustment = await self._load_adjust_context(
            organization_id=organization_id,
            interpretation_id=interpretation_id,
            instruction=instruction,
            actor_id=actor_id,
        )
        stored, key, _prepared = await self._prepare_launch(
            source=source,
            catalog=catalog,
            identity=identity,
            model=resolved_model,
            parameters=resolved,
            previous=self._previous_interpretation_summary(parent),
            adjustment=adjustment,
            parent_id=interpretation_id,
            regeneration_nonce=None,
        )
        if stored is not None:
            return InterpretationLaunch(
                status="stored", execution_key=key, stored=stored, job_name="", job_kwargs={}
            )
        return InterpretationLaunch(
            status="queued",
            execution_key=key,
            stored=None,
            job_name="adjust_skill_interpretation_job",
            job_kwargs={
                "organization_id": str(organization_id),
                "interpretation_id": str(interpretation_id),
                "instruction": instruction,
                "actor_id": str(actor_id),
                "model": resolved_model,
                "parameters": resolved,
                "execution_key": key,
            },
        )

    async def _load_adjust_context(
        self,
        *,
        organization_id: UUID,
        interpretation_id: UUID,
        instruction: str,
        actor_id: UUID,
    ) -> tuple[StoredInterpretationExecution, StoredSkillSource, dict[str, Any]]:
        """Adjust の親検証・source 取得・adjustment 構築を begin/実行で共有する。"""

        async with self._session_factory() as session:
            parent = await SkillRepository(session).get_model_interpretation(
                organization_id=organization_id, interpretation_id=interpretation_id
            )
        if parent.status is not SkillInterpretationStatus.PREVIEW_READY:
            raise SkillInterpretationNotReadyError(
                f"SkillInterpretation is not preview-ready: {interpretation_id}"
            )
        async with self._session_factory() as session:
            source = await SkillRepository(session).get_source(
                organization_id=organization_id, skill_source_id=parent.skill_source_id
            )
        adjustment = {
            "instruction": instruction,
            "actor_id": str(actor_id),
            "parent_interpretation_id": str(interpretation_id),
        }
        return parent, source, adjustment

    async def find_terminal_execution(
        self, *, organization_id: UUID, execution_key: str
    ) -> tuple[UUID, str, str | None] | None:
        """SSE stream 接続時の即時回放用に、確定済み execution を軽量に引く。"""

        async with self._session_factory() as session:
            return await SkillRepository(session).find_terminal_by_execution_key(
                organization_id=organization_id, execution_key=execution_key
            )

    async def get_interpretation_execution(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredInterpretationExecution:
        """Model interpretation の実行 detail を親との構造化 diff 付きで取得する。"""

        async with self._session_factory() as session:
            stored = await SkillRepository(session).get_model_interpretation(
                organization_id=organization_id, interpretation_id=interpretation_id
            )
        return await self._attach_diff(stored)

    async def _prepare_launch(
        self,
        *,
        source: StoredSkillSource,
        catalog: CapabilityCatalogSnapshot,
        identity: InterpreterSystemSkillIdentity,
        model: str,
        parameters: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        adjustment: Mapping[str, Any] | None,
        parent_id: UUID | None,
        regeneration_nonce: str | None,
    ) -> tuple[StoredInterpretationExecution | None, str, _PreparedRequest]:
        """Model を呼ばない受理段階。unsafe は同期で FAILED を確定し、再利用は既存を返す。

        戻り値の stored が None の場合だけ model 実行が必要で、key はその idempotency key。
        prepared は model 実行と失敗記録に必要な決定的成果物一式。
        """

        package, analysis, request = await self._prepare_request(
            source, catalog, identity, previous=previous, adjustment=adjustment
        )
        prepared = _PreparedRequest(package=package, analysis=analysis, request=request)
        adjustment_value = dict(adjustment) if adjustment is not None else None
        if request is None:
            key = self._blocked_execution_key(
                source.organization_id,
                package,
                analysis,
                catalog,
                identity,
                model,
                parameters,
            )
            stored = await self._finalize(
                self._failure_command(
                    source, package, analysis, catalog, identity, model, parameters, key,
                    InterpreterErrorCode.UNSAFE_SOURCE, parent_id, adjustment_value,
                )
            )
            return stored, key, prepared
        key = compute_execution_key(
            request,
            model=model,
            parameters=parameters,
            scope_id=str(source.organization_id),
            nonce=regeneration_nonce,
        )
        if regeneration_nonce is None:
            # 同一 frozen identity は既存の成功/失敗 record を返し、model の非決定性と重複課金を
            # API の再 click へ露出させない。明示 regeneration だけが nonce でこの分岐を迂回する。
            async with self._session_factory() as session:
                existing = await SkillRepository(session).find_model_interpretation(
                    organization_id=source.organization_id,
                    skill_source_id=source.skill_source_id,
                    interpreter_version=identity.interpreter_version,
                    execution_key=key,
                )
            if existing is not None:
                return await self._attach_diff(existing), key, prepared
        return None, key, prepared

    async def _run_interpretation(
        self,
        *,
        source: StoredSkillSource,
        interpreter: SkillInterpreter,
        catalog: CapabilityCatalogSnapshot,
        identity: InterpreterSystemSkillIdentity,
        model: str,
        parameters: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        adjustment: Mapping[str, Any] | None,
        parent_id: UUID | None,
        regeneration_nonce: str | None,
        on_event: InterpretProgressCallback | None = None,
    ) -> StoredInterpretationExecution:
        """初回 interpret と reinterpretation が共有する実行・検証・永続化の中核。"""

        # 受理段階(unsafe 確定・再利用)は begin_* と同じ単一実装を通す。
        stored_early, key, prepared = await self._prepare_launch(
            source=source,
            catalog=catalog,
            identity=identity,
            model=model,
            parameters=parameters,
            previous=previous,
            adjustment=adjustment,
            parent_id=parent_id,
            regeneration_nonce=regeneration_nonce,
        )
        if stored_early is not None:
            await _emit_terminal(on_event, stored_early)
            return stored_early
        package, analysis, request = prepared.package, prepared.analysis, prepared.request
        if request is None:  # pragma: no cover - prepare は決定的で直前の分岐が処理済み。
            raise RuntimeError("Unsafe source must have been finalized during preparation")
        adjustment_value = dict(adjustment) if adjustment is not None else None
        await _emit(on_event, "interpret.started", {
            "model": model,
            "skill_source_id": str(source.skill_source_id),
            "parent_interpretation_id": str(parent_id) if parent_id else None,
        })
        validation_attempts: list[str] = []
        validation_feedback: str | None = None
        validated: dict[str, Any] | None = None
        for attempt in range(_MAX_CANDIDATE_REPAIR_ATTEMPTS + 1):
            try:
                response = await interpreter.interpret(
                    request,
                    model=model,
                    parameters=parameters,
                    validation_feedback=validation_feedback,
                    on_event=on_event,
                )
            except InterpreterExecutionError as error:
                repair_feedback = candidate_repair_feedback(error.code)
                if repair_feedback is not None and attempt < _MAX_CANDIDATE_REPAIR_ATTEMPTS:
                    validation_attempts.append(repair_feedback)
                    log_event(
                        logger,
                        logging.WARNING,
                        "skill.interpret.candidate_invalid",
                        skill_source_id=str(source.skill_source_id),
                        execution_key=key,
                        parent_interpretation_id=str(parent_id) if parent_id else None,
                        attempt=attempt + 1,
                        error_code=error.code.value,
                    )
                    validation_feedback = repair_feedback
                    continue
                # structured_output_unavailable / timeout / provider 等は同一 request の再生成で
                # 直る根拠がなく、障害分類を保つため fail-fast とする。
                stored = await self._finalize(
                    self._failure_command(
                        source, package, analysis, catalog, identity, model, parameters, key,
                        error.code, parent_id, adjustment_value,
                        validation_attempts=validation_attempts,
                    )
                )
                await _emit_terminal(on_event, stored)
                return stored
            try:
                # model 経路は identity を platform 権威値で stamp する(model に複刻を強いない)。
                validated = self._runner().run(request, response, bind_identity=True)
                break
            except (ValidationError, ValueError, UnsafeSkillSourceError) as error:
                # Candidate 本文ではなく脱敏済み path/validator だけを retry と監査へ渡す。
                detail = _schema_failure_detail(error)
                validation_attempts.append(detail)
                log_event(
                    logger,
                    logging.WARNING,
                    "skill.interpret.schema_invalid",
                    skill_source_id=str(source.skill_source_id),
                    execution_key=key,
                    parent_interpretation_id=str(parent_id) if parent_id else None,
                    attempt=attempt + 1,
                    detail=detail,
                )
                if attempt < _MAX_CANDIDATE_REPAIR_ATTEMPTS:
                    validation_feedback = detail
                    continue
                stored = await self._finalize(
                    self._failure_command(
                        source, package, analysis, catalog, identity, model, parameters, key,
                        InterpreterErrorCode.SCHEMA_VALIDATION_FAILED,
                        parent_id,
                        adjustment_value,
                        detail=detail,
                        validation_attempts=validation_attempts,
                    )
                )
                await _emit_terminal(on_event, stored)
                return stored
        if validated is None:  # pragma: no cover - loop always returns or assigns.
            raise RuntimeError("Interpreter validation loop ended without a result")
        stored = await self._finalize(
            self._success_command(
                source, package, analysis, catalog, identity, model, parameters, key, validated,
                parent_id, adjustment_value, validation_attempts=validation_attempts,
            )
        )
        await _emit_terminal(on_event, stored)
        return stored

    def _require_interpreter(
        self,
    ) -> tuple[SkillInterpreter, CapabilityCatalogSnapshot, InterpreterSystemSkillIdentity]:
        """Interpreter/catalog/identity が全て配線済みであることを保証する。"""

        interpreter = self._interpreter
        catalog = self._capability_catalog
        identity = self._interpreter_identity
        if interpreter is None or catalog is None or identity is None:
            raise SkillInterpreterUnavailableError("Skill model interpreter is not configured")
        return interpreter, catalog, identity

    def _resolve_model(self, model: str | None) -> str:
        """明示 override か server 既定 model を解決し、未設定なら閉じる。"""

        resolved = model or self._default_model
        if not resolved:
            raise SkillInterpreterUnavailableError("Skill interpreter model is not configured")
        return resolved

    async def _finalize(
        self, command: SaveModelInterpretationCommand
    ) -> StoredInterpretationExecution:
        """Execution record を保存し、親がある場合は構造化 diff を付与して返す。"""

        return await self._attach_diff(await self._save_execution(command))

    async def _attach_diff(
        self, stored: StoredInterpretationExecution
    ) -> StoredInterpretationExecution:
        """親 interpretation を読み、dimension 別の revision diff を実行結果へ付ける。"""

        if stored.parent_interpretation_id is None:
            return stored
        async with self._session_factory() as session:
            parent = await SkillRepository(session).get_model_interpretation(
                organization_id=stored.organization_id,
                interpretation_id=stored.parent_interpretation_id,
            )
        diff = diff_interpretations(
            parent_manifest=parent.preview.runtime_manifest_draft,
            child_manifest=stored.preview.runtime_manifest_draft,
            parent_report=parent.report,
            child_report=stored.report,
        )
        return replace(stored, diff=diff)

    def _previous_interpretation_summary(
        self, parent: StoredInterpretationExecution
    ) -> dict[str, Any]:
        """親 interpretation の要点だけを prompt 用に compact 化する。"""

        manifest = parent.preview.runtime_manifest_draft
        identity = manifest.get("identity") if isinstance(manifest, dict) else {}
        confidence = parent.report.get("confidence") if isinstance(parent.report, dict) else None
        return {
            "interpretation_id": str(parent.interpretation_id),
            "execution_key": parent.execution_key,
            "compatibility_level": parent.compatibility_level,
            "summary": parent.summary,
            "confidence": confidence,
            "manifest_identity": identity if isinstance(identity, dict) else {},
        }

    async def create_version_draft(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredSkillVersion:
        """Interpretation を実 ID に再 binding し、gate report 付き DRAFT へ固定する。"""

        async with self._session_factory() as session, session.begin():
            repository = SkillRepository(session)
            interpretation = await repository.get_interpretation(
                organization_id=organization_id,
                interpretation_id=interpretation_id,
            )
            # 失敗や中間状態の interpretation から発行可能な DRAFT を作らせない。
            if (
                interpretation.interpretation_status
                is not SkillInterpretationStatus.PREVIEW_READY
            ):
                raise SkillInterpretationNotReadyError(
                    f"SkillInterpretation is not preview-ready: {interpretation_id}"
                )
            manifest = normalize_runtime_manifest(
                deepcopy(interpretation.preview.runtime_manifest_draft)
            )
            identity = manifest.get("identity")
            if not isinstance(identity, dict):
                raise ValueError("RuntimeManifest identity must be an object")
            previous_interpretation_id = identity.get("interpretation_id")
            identity["interpretation_id"] = str(interpretation_id)
            # 蓝图は manifest と同じ identity を持つ。凍結時に片方だけ再 stamp すると、
            # 発行済み version の監査値が互いに食い違ったまま不変化してしまう。
            bind_blueprint_identity(manifest)
            source = await repository.get_source(
                organization_id=organization_id,
                skill_source_id=interpretation.skill_source_id,
            )
            passed, findings = self._manifest_validator.evaluate(
                manifest,
                source_hash=interpretation.source_hash,
                interpretation_id=interpretation_id,
                source_files=source.source_files,
            )
            checksum = f"sha256:{sha256_hex(canonical_json(manifest))}"
            return await repository.create_version_draft(
                CreateSkillVersionDraftCommand(
                    organization_id=organization_id,
                    interpretation_id=interpretation_id,
                    manifest=manifest,
                    manifest_checksum=checksum,
                    gate_passed=passed,
                    gate_findings=findings,
                    interpretation_diff={
                        "identity.interpretation_id": {
                            "draft": previous_interpretation_id,
                            "frozen": str(interpretation_id),
                        },
                        "declared_tools": interpretation.preview.normalized_package.get(
                            "declared_tools", []
                        ),
                        "mapped_tools": manifest.get("tools", []),
                    },
                )
            )

    async def get_skill_version(
        self, *, organization_id: UUID, skill_version_id: UUID
    ) -> StoredSkillVersion:
        """Organization-scoped frozen SkillVersion detail を取得する。"""

        async with self._session_factory() as session:
            return await SkillRepository(session).get_skill_version(
                organization_id=organization_id,
                skill_version_id=skill_version_id,
            )

    async def list_skill_versions(
        self, *, organization_id: UUID
    ) -> tuple[StoredSkillVersion, ...]:
        """Organization の Skill library に属する全 frozen version を列挙する。"""

        async with self._session_factory() as session:
            return await SkillRepository(session).list_skill_versions(
                organization_id=organization_id
            )

    async def list_published_tasks(
        self, *, project_id: UUID
    ) -> tuple[PublishedTaskDescriptor, ...]:
        """PUBLISHED task catalog を投影し、Project の資源保有状況で就緒度を付与する。

        同じ SkillVersion でも束縛できる資源は Project ごとに異なるため、就緒度は catalog を
        引いてから算出する。Catalog 未配線の環境では readiness を None のままにし、資源が
        無いことを「実行不可」と誤って断定しない。
        """

        async with self._session_factory() as session:
            descriptors = await SkillRepository(session).list_published_task_descriptors(
                project_id=project_id
            )
        if self._resource_catalog is None or not descriptors:
            return descriptors
        candidates = await self._resource_catalog.candidates(project_id=project_id)
        return tuple(
            replace(
                descriptor,
                readiness=evaluate_blueprint_readiness(
                    descriptor.capability_blueprint,
                    candidates=candidates,
                    registered_capabilities=self._registered_capabilities,
                    registered_write_capabilities=self._registered_write_capabilities,
                    installed_provider_capabilities=self._installed_provider_capabilities,
                ),
            )
            for descriptor in descriptors
        )

    async def resolve_task_run(
        self,
        *,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
        input_json: Mapping[str, Any],
    ) -> ResolvedTaskRun:
        """PUBLISHED task を精確 version へ束縛し、入力を task の input schema で検証する。

        version が Project 所有かつ PUBLISHED でなければ 404 相当、task_key が Manifest に
        無ければ同じく not found、入力が schema 非適合なら TaskInputInvalidError を送出する。
        """

        async with self._session_factory() as session:
            repository = SkillRepository(session)
            skill, version, manifest = await repository.get_published_task_binding(
                project_id=project_id, skill_version_id=skill_version_id
            )
        resolved = resolve_task_run_from_manifest(
            skill_id=skill.id,
            skill_version_id=version.id,
            skill_key=skill.key,
            version=version.version,
            manifest_checksum=manifest.checksum,
            manifest=manifest.manifest_json,
            task_key=task_key,
        )
        if resolved is None:
            raise PublishedTaskNotFoundError(
                f"Published task not found: {skill_version_id}/{task_key}"
            )
        input_schema = deepcopy(resolved.input_schema)
        output_schema = deepcopy(resolved.output_schema)
        self._validate_task_input(input_schema, input_json)
        skill_snapshot = deepcopy(resolved.skill_snapshot)
        # Manifest の generated contract だけを Run 作成へ渡し、source/image の business file を
        # 再解決する経路を作らない。
        skill_snapshot["contract_schemas"] = {
            resolved.input_schema_checksum: input_schema,
            resolved.output_schema_checksum: output_schema,
        }
        if (
            resolved.task_output_schema is not None
            and resolved.task_output_schema_checksum is not None
        ):
            skill_snapshot["contract_schemas"][resolved.task_output_schema_checksum] = deepcopy(
                resolved.task_output_schema
            )
        return replace(
            resolved,
            input_schema=input_schema,
            output_schema=output_schema,
            skill_snapshot=skill_snapshot,
        )

    def _validate_task_input(
        self, schema: Mapping[str, Any], input_json: Mapping[str, Any]
    ) -> None:
        """Run 入力を published SkillVersion の inline input schema で決定的に検証する。"""

        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(dict(input_json)), key=lambda item: list(item.path))
        if errors:
            detail = "; ".join(
                f"{_json_pointer(error.path)}: {error.message}" for error in errors[:5]
            )
            raise TaskInputInvalidError(f"Run input does not match task input schema: {detail}")

    async def publish_skill_version(
        self,
        *,
        organization_id: UUID,
        skill_version_id: UUID,
        published_by: UUID,
        accepted_warnings: frozenset[str],
    ) -> StoredSkillVersion:
        """Repository hard gate を迂回せず SkillVersion を publish する。"""

        async with self._session_factory() as session, session.begin():
            return await SkillRepository(session).publish_skill_version(
                organization_id=organization_id,
                skill_version_id=skill_version_id,
                published_by=published_by,
                accepted_warnings=accepted_warnings,
            )

    async def deprecate_skill_version(
        self, *, organization_id: UUID, skill_version_id: UUID
    ) -> StoredSkillVersion:
        """Organization の PUBLISHED 版を廃止し、新規 discovery/Run を閉じる。"""

        async with self._session_factory() as session, session.begin():
            return await SkillRepository(session).deprecate_skill_version(
                organization_id=organization_id,
                skill_version_id=skill_version_id,
            )

    async def delete_skill_version(
        self, *, organization_id: UUID, skill_version_id: UUID
    ) -> None:
        """監査参照のない DEPRECATED 版を library から物理削除する。"""

        async with self._session_factory() as session, session.begin():
            await SkillRepository(session).delete_skill_version(
                organization_id=organization_id,
                skill_version_id=skill_version_id,
            )

    async def enable_project_skill_version(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        enabled_by: UUID,
    ) -> StoredProjectSkillVersion:
        """PUBLISHED 精確版を Project の discovery 集合へ追加する。"""

        async with self._session_factory() as session, session.begin():
            return await SkillRepository(session).enable_project_skill_version(
                organization_id=organization_id,
                project_id=project_id,
                skill_version_id=skill_version_id,
                enabled_by=enabled_by,
            )

    async def disable_project_skill_version(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
    ) -> StoredProjectSkillVersion:
        """Project の version discovery を停用し、既存 Run snapshot は変更しない。"""

        async with self._session_factory() as session, session.begin():
            return await SkillRepository(session).disable_project_skill_version(
                organization_id=organization_id,
                project_id=project_id,
                skill_version_id=skill_version_id,
            )

    async def list_project_skill_versions(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        include_disabled: bool = False,
    ) -> tuple[StoredProjectSkillVersion, ...]:
        """Project の active または全履歴の version 有効化関係を列挙する。"""

        async with self._session_factory() as session:
            return await SkillRepository(session).list_project_skill_versions(
                organization_id=organization_id,
                project_id=project_id,
                include_disabled=include_disabled,
            )

    async def _prepare_request(
        self,
        source: StoredSkillSource,
        catalog: CapabilityCatalogSnapshot,
        identity: InterpreterSystemSkillIdentity,
        *,
        previous: Mapping[str, Any] | None = None,
        adjustment: Mapping[str, Any] | None = None,
    ) -> tuple[NormalizedSkillPackage, SkillStaticAnalysis, dict[str, Any] | None]:
        """保存済み snapshot を再正規化し、静的解析と frozen request を offline で作る。"""

        with TemporaryDirectory(prefix="projectmind-interpret-") as temporary:
            root = Path(temporary).resolve()
            await self._materialize_source(source, root)
            package = self._parser.parse_directory(root)
            analysis = SkillStaticAnalyzer().analyze(package, load_inline_text_files(root, package))
        if package.content_hash != source.source_hash:
            raise SkillSourceIntegrityError(
                "Stored SkillSource content hash drifted from its snapshot"
            )
        try:
            request = build_interpreter_request(
                package=package,
                analysis=analysis,
                catalog=catalog,
                system_skill=identity,
                previous_interpretation=previous,
                adjustment=adjustment,
            )
        except UnsafeSkillSourceError:
            return package, analysis, None
        return package, analysis, request

    async def _materialize_source(self, source: StoredSkillSource, root: Path) -> None:
        """保存時の storage URI に応じて、元の source bytes を一時 root へ再構築する。"""

        if source.storage_uri.startswith("database://") or not source.storage_uri:
            _write_inline_skill_files(source.source_files, root)
            return
        if not source.storage_uri.startswith("s3://"):
            raise SkillSourceIntegrityError("Stored SkillSource storage URI is invalid")
        await self._materialize_stored_bundle(source, root)

    async def _materialize_stored_bundle(self, source: StoredSkillSource, root: Path) -> None:
        """Object storage の全 file を per-file checksum 検証後に source root へ展開する。"""

        storage = self._file_storage
        if storage is None:
            raise SkillStorageUnavailableError(
                "Object storage is not configured for stored SkillSource reconstruction"
            )
        prefix = _storage_uri_prefix(source.storage_uri, self._storage_bucket)
        if not source.source_file_index:
            raise SkillSourceIntegrityError("Stored SkillSource file manifest is missing")
        seen: set[str] = set()
        for item in source.source_file_index:
            path, expected_size, expected_hash = _validate_source_file_index(item, seen)
            try:
                data = await storage.get(f"{prefix}/{path}")
            except FileStorageError as error:
                # Storage adapter は BlobNotFoundError を含む backend error を返す。本文をログへ
                # 出さず、source を再利用できない不変性違反として上位へ伝える。
                raise SkillSourceIntegrityError(
                    f"Stored SkillSource file is unavailable: {path}"
                ) from error
            actual_hash = f"sha256:{sha256_hex(data)}"
            if len(data) != expected_size or actual_hash != expected_hash:
                raise SkillSourceIntegrityError(
                    f"Stored SkillSource file checksum drifted: {path}"
                )
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise SkillSourceIntegrityError("Stored SkillSource file path escapes source root")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def _blocked_execution_key(
        self,
        organization_id: UUID,
        package: NormalizedSkillPackage,
        analysis: SkillStaticAnalysis,
        catalog: CapabilityCatalogSnapshot,
        identity: InterpreterSystemSkillIdentity,
        model: str,
        parameters: Mapping[str, Any],
    ) -> str:
        """危険 source でも識別子から決定的な execution key を得て失敗を idempotent 化する。"""

        pseudo_request = {
            "source": {"content_hash": package.content_hash},
            "static_analysis": analysis.to_dict(),
            "capability_catalog": catalog.to_dict(),
            "interpreter": identity.to_dict(),
            "blocked": True,
        }
        return compute_execution_key(
            pseudo_request,
            model=model,
            parameters=parameters,
            scope_id=str(organization_id),
        )

    def _success_command(
        self,
        source: StoredSkillSource,
        package: NormalizedSkillPackage,
        analysis: SkillStaticAnalysis,
        catalog: CapabilityCatalogSnapshot,
        identity: InterpreterSystemSkillIdentity,
        model: str,
        parameters: Mapping[str, Any],
        execution_key: str,
        validated: Mapping[str, Any],
        parent_id: UUID | None,
        adjustment: dict[str, Any] | None,
        *,
        validation_attempts: Sequence[str] = (),
    ) -> SaveModelInterpretationCommand:
        """検証済み response を PREVIEW_READY の不変 record command へ写像する。"""

        report = cast(dict[str, Any], validated["report"])
        manifest = cast(dict[str, Any], validated["runtime_manifest_draft"])
        interpreter = cast(dict[str, Any], validated["interpreter"])
        raw_confidence = report.get("confidence")
        confidence_values = (
            [float(value) for value in raw_confidence.values()]
            if isinstance(raw_confidence, dict)
            else []
        )
        compatibility = manifest.get("compatibility")
        fallback_confidence = (
            float(compatibility.get("confidence", 0.5))
            if isinstance(compatibility, dict)
            else 0.5
        )
        return SaveModelInterpretationCommand(
            organization_id=source.organization_id,
            skill_source_id=source.skill_source_id,
            execution_key=execution_key,
            interpreter_version=identity.interpreter_version,
            model=model,
            status=SkillInterpretationStatus.PREVIEW_READY,
            compatibility_level=str(report["compatibility_level"]),
            confidence=min(confidence_values, default=fallback_confidence),
            summary=str(report["summary"]),
            assumptions=tuple(report.get("assumptions", [])),
            questions=tuple(report.get("questions", [])),
            diagnostics=tuple(report.get("diagnostics", [])),
            normalized_package=package.to_dict(),
            manifest_draft=manifest,
            report=report,
            execution={
                "model": model,
                "parameters": dict(parameters),
                "prompt_checksum": interpreter["prompt_checksum"],
                "response_version": validated["response_version"],
                "catalog_checksum": catalog.checksum,
                "static_analysis_checksum": analysis.checksum,
                "contract_test_drafts": validated.get("contract_test_drafts", []),
                "validation_attempts": list(validation_attempts),
                "error_code": None,
            },
            parent_interpretation_id=parent_id,
            adjustment=adjustment,
        )

    def _failure_command(
        self,
        source: StoredSkillSource,
        package: NormalizedSkillPackage,
        analysis: SkillStaticAnalysis,
        catalog: CapabilityCatalogSnapshot,
        identity: InterpreterSystemSkillIdentity,
        model: str,
        parameters: Mapping[str, Any],
        execution_key: str,
        code: InterpreterErrorCode,
        parent_id: UUID | None,
        adjustment: dict[str, Any] | None,
        detail: str | None = None,
        *,
        validation_attempts: Sequence[str] = (),
    ) -> SaveModelInterpretationCommand:
        """失敗を Secret や来源本文を含まない FAILED 監査 record command へ写像する。"""

        # detail は構造化 path/validator など脱敏済みの短い内訳のみ (来源値は含めない)。
        execution: dict[str, Any] = {
            "model": model,
            "parameters": dict(parameters),
            "error_code": code.value,
            "catalog_checksum": catalog.checksum,
            "static_analysis_checksum": analysis.checksum,
            "validation_attempts": list(validation_attempts),
        }
        if detail is not None:
            execution["detail"] = detail
        return SaveModelInterpretationCommand(
            organization_id=source.organization_id,
            skill_source_id=source.skill_source_id,
            execution_key=execution_key,
            interpreter_version=identity.interpreter_version,
            model=model,
            status=SkillInterpretationStatus.FAILED,
            compatibility_level="assisted",
            confidence=0.0,
            summary=f"Model interpretation failed: {code.value}",
            assumptions=(),
            questions=(),
            diagnostics=(),
            normalized_package=package.to_dict(),
            manifest_draft={},
            report=None,
            execution=execution,
            parent_interpretation_id=parent_id,
            adjustment=adjustment,
        )

    async def _save_execution(
        self, command: SaveModelInterpretationCommand
    ) -> StoredInterpretationExecution:
        """Execution record を一つの transaction で idempotent に永続化する。"""

        return await save_execution_idempotently(self._session_factory, command)

    def _runner(self) -> InterpreterFixtureRunner:
        """Response 検証に使う InterpreterFixtureRunner を遅延生成して共有する。"""

        if self._fixture_runner is None:
            self._fixture_runner = InterpreterFixtureRunner(self._contracts_dir)
        return self._fixture_runner


async def save_execution_idempotently(
    session_factory: async_sessionmaker[AsyncSession],
    command: SaveModelInterpretationCommand,
) -> StoredInterpretationExecution:
    """同一 execution key の並行保存で uq 制約に負けた側も、勝者の record を再利用して成功させる。

    check-then-insert は transaction 間の競合を防げないため、IntegrityError を
    冪等再読の合図として扱う。key が存在しない IntegrityError(例: lineage FK 違反)は
    保存の失敗であり、そのまま呼び出し元へ伝播させる。
    """

    try:
        async with session_factory() as session, session.begin():
            return await SkillRepository(session).save_model_interpretation(command)
    except IntegrityError:
        async with session_factory() as session:
            existing = await SkillRepository(session).find_model_interpretation(
                organization_id=command.organization_id,
                skill_source_id=command.skill_source_id,
                interpreter_version=command.interpreter_version,
                execution_key=command.execution_key,
            )
            if existing is None:
                raise
            return existing


def _write_inline_skill_files(files: Sequence[InlineSkillFile], root: Path) -> None:
    """Browser 入力を root 配下へ限定し、同名 path の曖昧さを拒否して書き込む。"""

    seen: set[str] = set()
    for file in files:
        relative = _safe_skill_file_path(file.path)
        if relative in seen:
            raise SkillImportError(
                "duplicate_file_path",
                "Skill source contains duplicate file paths",
                path=relative,
            )
        seen.add(relative)
        target = (root / relative).resolve()
        # 一時 directory への書き込みでも、入力 path の正規化漏れで root 外へ出ないことを保証する。
        if not target.is_relative_to(root):
            raise SkillImportError(
                "invalid_file_path",
                "Skill source file path must stay inside the source root",
                path=relative,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        # parser の source hash は UTF-8 bytes を対象にするため、text writer の改行変換を避ける。
        target.write_bytes(file.content.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    """受理段階で構築した決定的成果物。model 実行と失敗記録が再利用する。"""

    package: NormalizedSkillPackage
    analysis: SkillStaticAnalysis
    request: dict[str, Any] | None


def _schema_failure_detail(error: Exception) -> str:
    """契約検証失敗の内訳を脱敏した短い診断文字列にする。

    ValidationError と TaskContractCompilationError は path/code だけを残し、instance 値
    (model/来源由来のため秘匿) は載せない。その他の ValueError は platform 固定 message のみ。
    """

    if isinstance(error, TaskContractCompilationError):
        return f"{error.path}: {error.code}"
    if isinstance(error, ValidationError):
        pointer = "/" + "/".join(str(part) for part in error.absolute_path)
        detail = f"{pointer}: {error.validator}"
        # additionalProperties は「どの余分な key か」で直せるので、値ではなく key 名だけ添える。
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            allowed = (
                set(error.schema.get("properties", {}))
                if isinstance(error.schema, dict)
                else set()
            )
            unexpected = sorted(key for key in error.instance if key not in allowed)
            if unexpected:
                return f"{detail} unexpected={unexpected[:8]}"
        # validator_value は契約側 (schema) の期待値で来源値ではないため、短い時だけ添える。
        try:
            constraint = json.dumps(error.validator_value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            constraint = ""
        return f"{detail} {constraint}" if 0 < len(constraint) <= 120 else detail
    return str(error)


async def _emit(
    on_event: InterpretProgressCallback | None, event: str, data: Mapping[str, Any]
) -> None:
    """進行 callback が指定された時だけ event を転送する。"""

    if on_event is not None:
        await on_event(event, data)


async def _emit_terminal(
    on_event: InterpretProgressCallback | None, stored: StoredInterpretationExecution
) -> None:
    """確定した interpretation record を completed/failed の終端 event として転送する。"""

    if on_event is None:
        return
    data = {
        "interpretation_id": str(stored.interpretation_id),
        "status": stored.status.value,
        "error_code": stored.error_code,
        "compatibility_level": stored.compatibility_level,
        "reused": stored.reused,
    }
    event = (
        "interpret.completed"
        if stored.status is SkillInterpretationStatus.PREVIEW_READY
        else "interpret.failed"
    )
    await on_event(event, data)


def _write_upload_skill_files(files: Sequence[UploadSkillFile], root: Path) -> None:
    """Upload された bytes を root 配下へ限定し、重複 path を拒否して書き込む。"""

    seen: set[str] = set()
    for file in files:
        relative = _safe_skill_file_path(file.path)
        if relative in seen:
            raise SkillImportError(
                "duplicate_file_path",
                "Skill source contains duplicate file paths",
                path=relative,
            )
        seen.add(relative)
        target = (root / relative).resolve()
        # 入力 path の正規化漏れで root 外へ書き込まないことを一時 directory でも保証する。
        if not target.is_relative_to(root):
            raise SkillImportError(
                "invalid_file_path",
                "Skill source file path must stay inside the source root",
                path=relative,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.data)


def _normalize_upload_skill_root(
    files: tuple[UploadSkillFile, ...],
) -> tuple[UploadSkillFile, ...]:
    """Browser directory 選択が付与する共通 root 名だけを除去する。

    直下に SKILL.md が既にある入力は変更しない。全 file が同じ先頭 directory を共有し、
    その directory を除去した結果だけが SKILL.md を root に置く場合に限定することで、通常の
    nested resource directory を誤って source root と解釈しない。
    """

    if not files:
        return files
    safe_paths = tuple(_safe_skill_file_path(file.path) for file in files)
    if "SKILL.md" in safe_paths:
        return files
    parts = tuple(PurePosixPath(path).parts for path in safe_paths)
    common_root = parts[0][0]
    if any(len(item) < 2 or item[0] != common_root for item in parts):
        return files
    stripped = tuple(PurePosixPath(*item[1:]).as_posix() for item in parts)
    if "SKILL.md" not in stripped:
        return files
    return tuple(replace(file, path=path) for file, path in zip(files, stripped, strict=True))


def _upload_text_source_files(
    package: NormalizedSkillPackage, root: Path
) -> tuple[InlineSkillFile, ...]:
    """Parser が text と判定した file だけを DB snapshot 用の InlineSkillFile に戻す。

    binary asset は snapshot に載せず object storage の bundle に委ねる。text/binary の判定は
    parser を唯一の権威とし、ここでは複製しない。
    """

    text_files: list[InlineSkillFile] = []
    for file in package.files:
        if file.binary:
            continue
        # hash は raw bytes を対象にするため、read_text の newline 変換で CRLF を壊さない。
        text = (root / file.path).read_bytes().decode("utf-8")
        text_files.append(InlineSkillFile(path=file.path, content=text))
    return tuple(text_files)


async def _store_upload_bundle(
    storage: FileStorage, key_prefix: str, files: Sequence[UploadSkillFile]
) -> None:
    """Upload の raw bundle (binary 含む) を content-hash 由来の key で保存する。"""

    for file in files:
        relative = _safe_skill_file_path(file.path)
        await storage.put(
            f"{key_prefix}/{relative}",
            file.data,
            content_type=file.content_type or "application/octet-stream",
        )


def _safe_skill_file_path(value: str) -> str:
    """POSIX 形式の相対 file path だけを API input として受理する。"""

    candidate = value.replace("\\", "/").strip()
    path = PurePosixPath(candidate)
    if path.is_absolute() or not candidate or candidate.endswith("/"):
        raise SkillImportError(
            "invalid_file_path",
            "Skill source file path must be a relative file path",
            path=value,
        )
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SkillImportError(
            "invalid_file_path",
            "Skill source file path must not contain dot segments",
            path=value,
        )
    return path.as_posix()


def _save_command(
    *,
    organization_id: UUID,
    imported_by: UUID,
    files: tuple[InlineSkillFile, ...],
    preview: SkillPreview,
    storage_uri: str | None = None,
) -> SaveSkillPreviewCommand:
    """Parser output の固定 field を persistence command へ抽出する。"""

    package = preview.normalized_package
    manifest = preview.runtime_manifest_draft
    source = _mapping(package, "source")
    metadata = _mapping(package, "metadata")
    identity = _mapping(manifest, "identity")
    compatibility = _mapping(manifest, "compatibility")
    extensions = _mapping(manifest, "extensions")
    diagnostics = compatibility.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(isinstance(item, dict) for item in diagnostics):
        raise RuntimeError("RuntimeManifest diagnostics must be an object array")
    confidence = compatibility.get("confidence")
    if not isinstance(confidence, int | float):
        raise RuntimeError("RuntimeManifest confidence must be numeric")
    return SaveSkillPreviewCommand(
        organization_id=organization_id,
        imported_by=imported_by,
        name=_string(metadata, "name"),
        source_type=_string(source, "type"),
        source_hash=_string(source, "content_hash"),
        source_files=files,
        interpreter_version=_string(identity, "interpreter_version"),
        compatibility_level=_string(compatibility, "level"),
        confidence=float(confidence),
        diagnostics=tuple(cast(dict[str, Any], item) for item in diagnostics),
        checksum=_string(extensions, "normalized_package_hash"),
        preview=preview,
        storage_uri=storage_uri,
        source_file_index=_package_file_index_from_source(source),
    )


def _package_file_index_from_source(source: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Parser の source.files を型検証し、binary を含む file index を返す。"""

    files = source.get("files")
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise RuntimeError("Normalized package source.files must be an object array")
    return tuple(dict(cast(dict[str, Any], item)) for item in files)


def _storage_uri_prefix(storage_uri: str, bucket: str) -> str:
    """保存時 bucket と一致する s3 URI から安全な object key prefix を取り出す。"""

    parsed = urlsplit(storage_uri)
    prefix = parsed.path.lstrip("/").rstrip("/")
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or not prefix
        or parsed.query
        or parsed.fragment
    ):
        raise SkillSourceIntegrityError("Stored SkillSource storage URI is invalid")
    try:
        return sanitize_object_key(prefix)
    except FileStorageError as error:
        raise SkillSourceIntegrityError("Stored SkillSource storage URI is invalid") from error


def _validate_source_file_index(
    item: Mapping[str, Any], seen: set[str]
) -> tuple[str, int, str]:
    """Persisted file index の path、size、checksum を検証し、重複を拒否する。"""

    path = item.get("path")
    size = item.get("size")
    checksum = item.get("sha256")
    if (
        not isinstance(path, str)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise SkillSourceIntegrityError("Stored SkillSource file manifest is invalid")
    if not isinstance(checksum, str) or not checksum.startswith("sha256:"):
        raise SkillSourceIntegrityError("Stored SkillSource file manifest is invalid")
    try:
        normalized = _safe_skill_file_path(path)
    except SkillImportError as error:
        raise SkillSourceIntegrityError("Stored SkillSource file manifest is invalid") from error
    if normalized in seen:
        raise SkillSourceIntegrityError("Stored SkillSource file manifest contains duplicates")
    seen.add(normalized)
    return normalized, size, checksum


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Parser output の object field を型付きで取得する。"""

    nested = value.get(key)
    if not isinstance(nested, dict):
        raise RuntimeError(f"Parser output field must be an object: {key}")
    return cast(dict[str, Any], nested)


def _string(value: Mapping[str, Any], key: str) -> str:
    """Parser output の必須 string field を取得する。"""

    item = value.get(key)
    if not isinstance(item, str):
        raise RuntimeError(f"Parser output field must be a string: {key}")
    return item


def _json_pointer(path: Any) -> str:
    """jsonschema error path を診断用の JSON Pointer 風文字列へ変換する。"""

    return "/" + "/".join(str(item) for item in path) if path else "(root)"
