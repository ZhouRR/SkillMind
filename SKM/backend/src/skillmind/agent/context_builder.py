"""ClaimedRun snapshot を検証済み RunContext へ変換する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.database_provider import DatabaseReadProvider
from skillmind.agent.domain import (
    MaterializedResource,
    RegisteredTool,
    RunContext,
    RunLimits,
)
from skillmind.agent.repository_source import (
    RepositoryBindingRef,
)
from skillmind.agent.runtime_policy import uses_modern_runtime
from skillmind.agent.skill_files import SKILL_FILE_CAPABILITIES
from skillmind.agent.task_brief import (
    build_agent_task_brief,
    render_task_brief_prompt,
    resolve_execution_profile,
)
from skillmind.agent.tool_gateway import (
    ToolRegistry,
)
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import WorkspaceMaterializer
from skillmind.documents.library import (
    DOCUMENT_LIBRARY_REVISION,
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryTarget,
    is_document_library_source,
    parse_document_library_reference,
    parse_document_library_source,
)
from skillmind.documents.snapshot import (
    DOCUMENT_CAPABILITIES,
    DOCUMENT_PROVIDER,
    document_preparation_policy,
    selected_document_snapshots,
)
from skillmind.effects.catalog import EFFECT_CAPABILITIES
from skillmind.effects.proposal import CHANGE_PROPOSE_CAPABILITY
from skillmind.effects.release import ExecutionFeatures
from skillmind.integrations.domain import ResourceBindingLevel, binding_checksum
from skillmind.runs.domain import ClaimedRun
from skillmind.runs.input_snapshot import InputFileSeal
from skillmind.runs.interaction import INTERACTION_REQUEST_CAPABILITY
from skillmind.runs.proposal_continuation import ProposalContinuationReader
from skillmind.skills.document_prerequisites import (
    DOCUMENT_READINESS_CAPABILITY,
    document_prerequisites,
)
from skillmind.skills.execution import resolve_skill_definition
from skillmind.skills.frozen_manifest import verified_run_manifest
from skillmind.skills.resource_binding import required_resource_keys


class ProductionRunContextBuilder:
    """Immutable Run snapshot と local runtime dependency から Agent context を構築する。"""

    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager,
        tool_registry: ToolRegistry,
        model: str | None,
        materializer: WorkspaceMaterializer | None = None,
        deferred_features_enabled: bool = True,
        database_writes_enabled: bool = False,
        document_writes_enabled: bool = False,
        git_writes_enabled: bool = False,
        mcp_tools_enabled: bool = False,
        document_library_target: DocumentLibraryTarget | None = None,
        proposal_continuations: ProposalContinuationReader | None = None,
        database_observations: DatabaseReadProvider | None = None,
    ) -> None:
        """Workspace、Tool と model の Worker 起動時 snapshot を保持する。

        ``materializer`` を渡すと Run 準備段階で冻结資源を input/ へ只読物化する (計画 §19 W3)。
        None のときは物化を行わない (offline/未配線環境の従来挙動)。
        """

        self._workspace_manager = workspace_manager
        self._tool_registry = tool_registry
        self._model = model
        self._materializer = materializer
        self._document_library_target = document_library_target
        self._proposal_continuations = proposal_continuations
        self._database_observations = database_observations
        self._execution_features = ExecutionFeatures(
            deferred_features_enabled, database_writes_enabled,
            document_writes_enabled, git_writes_enabled, mcp_tools_enabled
        )

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """Input/Schema/Source/Permission を再検証し、最小 Tool 付き context を返す。"""

        if self._model is None:
            # 実行時 model を CLI default へ暗黙委任すると Run の監査 snapshot が不完全になる。
            raise ValueError("ANTHROPIC_MODEL must be configured before dispatch")
        task = claimed_run.task_snapshot_json
        _required_string(task, "input_schema")
        _required_string(task, "output_schema")
        input_schema = _snapshot_schema(task, key="input_schema_json")
        output_schema = _snapshot_schema(task, key="output_schema_json")
        _validate_json(input_schema, claimed_run.input_json, label="Run input")
        permission = claimed_run.permission_snapshot_json
        _validate_actor(permission, claimed_run.actor_id)
        if permission.get("mode") != "auto_read_only":
            raise ValueError("M0 runtime requires auto_read_only permission mode")
        allowed = permission.get("allowed_capabilities")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("Permission snapshot contains invalid capabilities")
        # 旧権限を削って別 Run として実行せず、物化・モデル起動の前に拒否する。
        if any(
            not self._execution_features.capability_enabled(capability)
            for capability in allowed
        ):
            raise ValueError("Run snapshot requires disabled execution features")

        # 不変 SkillVersion snapshot を先に確定してから、その Manifest を根拠に Tool を解決する。
        # 業務固有 task に依存せず、任意 published task の Run を同一経路で実行する。
        manifest = _verified_manifest(claimed_run, task)
        blueprint = resolve_skill_definition(manifest)
        if blueprint is None:
            raise ValueError("Run SkillVersion Manifest does not declare a CapabilityBlueprint")
        if not self._execution_features.blueprint_enabled(blueprint):
            raise ValueError("Run Blueprint requires disabled execution features")
        prerequisites = document_prerequisites(manifest, str(task.get("task_key", "")))
        if prerequisites:
            if DOCUMENT_READINESS_CAPABILITY not in allowed:
                raise ValueError("Document prerequisites require readiness permission")
            snapshots = selected_document_snapshots(
                claimed_run.selected_sources_json, project_id=claimed_run.project_id
            )
            deferred_ids = {
                document.document_id for snapshot in snapshots
                if document_preparation_policy(
                    claimed_run.selected_sources_json[snapshot.requirement_key]
                )
                for document in snapshot.documents
            }
            if any(document.document_id not in deferred_ids
                   for snapshot in snapshots for document in snapshot.documents):
                raise ValueError("Document prerequisites require on-demand preparation")
        execution_profile = resolve_execution_profile(blueprint).profile.value
        snapshot_profile = permission.get("execution_profile")
        declares_workspace = any(
            isinstance(item, Mapping)
            and isinstance(item.get("capability"), str)
            and str(item["capability"]).startswith("workspace.")
            for item in _sequence(manifest.get("tools"))
        )
        if snapshot_profile is None and declares_workspace:
            # 旧 snapshot に後付けで workspace 権限を生やさず、新規 Run だけが profile を固定する。
            raise ValueError("Permission snapshot does not authorize workspace capabilities")
        if snapshot_profile is not None and snapshot_profile != execution_profile:
            raise ValueError("Permission snapshot execution profile does not match the Skill")
        tools, repository_bindings = _resolve_source_tools(
            claimed_run,
            manifest,
            self._tool_registry,
            allowed,
            execution_profile=execution_profile,
            document_library_target=self._document_library_target,
        )
        document_snapshots = selected_document_snapshots(
            claimed_run.selected_sources_json, project_id=claimed_run.project_id
        )

        snapshot = claimed_run.limits_snapshot_json
        limits = RunLimits(
            max_turns=_positive_int(snapshot, "max_turns"),
            wall_timeout_seconds=_positive_int(snapshot, "wall_timeout_seconds"),
            max_output_bytes=_positive_int(snapshot, "max_output_bytes"),
            max_budget_usd=_optional_positive_number(snapshot, "max_budget_usd"),
        )
        # Workspace を先に確定し、Agent 起動前に冻结資源を input/ へ只読物化する (計画 §19 W3)。
        # 物化は permission/Tool 解決の後に置き、許可されていない資源を先に落とさない。
        workspace = self._workspace_manager.initialize(claimed_run.run_id)
        materialized: tuple[MaterializedResource, ...] = ()
        skill_files: tuple[InputFileSeal, ...] = ()
        if self._materializer is not None:
            prepared_input = await self._materializer.materialize(
                claimed_run=claimed_run,
                workspace=workspace,
                project_id=claimed_run.project_id,
                run_id=claimed_run.run_id,
                blueprint=blueprint,
                repository_bindings=repository_bindings,
                document_snapshots=document_snapshots,
                skill_documents=(manifest.get("source_documents", ())
                    if any(tool.capability in SKILL_FILE_CAPABILITIES for tool in tools) else ()),
            )
            workspace = prepared_input.workspace
            materialized = prepared_input.resources
            skill_files = prepared_input.skill_files
        # Skill guidance は Brief を経由してのみ Agent へ届く。permission と Tool 解決を先に
        # 確定させてから組み立て、Brief が許可されていない能力を語らないようにする。
        brief = build_agent_task_brief(
            run_id=claimed_run.run_id,
            project_id=claimed_run.project_id,
            task_snapshot=task,
            manifest=manifest,
            selected_sources=claimed_run.selected_sources_json,
            tools=tools,
            limits=limits,
            model=self._model,
            segment_no=claimed_run.segment_no,
            segment_objective=_segment_objective(claimed_run.segment_objective_json),
            checkpoint=claimed_run.checkpoint_json,
            effect_receipts=(await self._proposal_continuations.receipts(claimed_run)
                if self._proposal_continuations is not None
                and uses_modern_runtime(task) else ()),
            database_observations=(await self._database_observations.project_facts(
                claimed_run, tools, workspace
            )
                if self._database_observations is not None else ()),
            # 物化器が実際に書いた落点だけを Brief へ載せる (計画 §19 W6)。未配線環境で存在
            # しない directory を案内すると、Agent は読めない path を試して行き詰まる。
            materialized=materialized,
            skill_files=skill_files,
        )
        return RunContext(
            run_id=claimed_run.run_id,
            run_attempt_id=claimed_run.run_attempt_id,
            project_id=claimed_run.project_id,
            user_id=claimed_run.actor_id,
            prompt=render_task_brief_prompt(
                brief.brief,
                input_json=claimed_run.input_json,
                output_schema=output_schema,
            ),
            task_snapshot=dict(task),
            skill_snapshots=(manifest,),
            resolved_sources=dict(claimed_run.selected_sources_json),
            permission_snapshot=dict(permission),
            workspace=workspace,
            limits=limits,
            result_schema=output_schema,
            tools=tuple(tools),
            model=self._model,
            sequence_start=sequence_start,
            trace={
                "attempt_no": claimed_run.attempt_no,
                "segment_no": claimed_run.segment_no,
                "continuation_mode": claimed_run.continuation_mode.value,
            },
            task_brief=brief.brief,
            task_brief_checksum=brief.checksum,
            input_json=dict(claimed_run.input_json),
            resolved_proposal=(
                await self._proposal_continuations.load(claimed_run)
                if self._proposal_continuations is not None else None
            ),
        )


def _segment_objective(value: Mapping[str, Any] | None) -> str | None:
    """Segment row の公開 objective text だけを Brief override として返す。"""

    if value is None:
        return None
    objective = value.get("text")
    return objective if isinstance(objective, str) and objective else None


def _snapshot_schema(task: Mapping[str, Any], *, key: str) -> dict[str, Any]:
    """Run に固定された generated Schema だけを読み、legacy Run の再実行を拒否する。"""

    frozen = task.get(key)
    if isinstance(frozen, dict):
        schema = dict(frozen)
        Draft202012Validator.check_schema(schema)
        return schema
    raise ValueError(f"Run task snapshot is missing frozen Schema: {key}")


def _validate_json(schema: Mapping[str, Any], value: Any, *, label: str) -> None:
    """Public 値を error へ含めず、Draft 2020-12 契約への不一致を拒否する。"""

    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        raise ValueError(f"{label} did not match its contract")


def _verified_manifest(claimed_run: ClaimedRun, task: Mapping[str, Any]) -> Mapping[str, Any]:
    """唯一の SkillVersion snapshot から不変 Manifest を取り出し checksum 同一性を検証する。

    Manifest checksum と task 側の記録が一致しなければ、凍結された published 内容から
    ずれているとみなして fail closed とする。実行に使う Manifest 契約の唯一の入口。
    """

    return verified_run_manifest(claimed_run.skill_snapshots_json, task)


def _resolve_source_tools(
    claimed_run: ClaimedRun,
    manifest: Mapping[str, Any],
    registry: ToolRegistry,
    allowed: Sequence[str],
    *,
    execution_profile: str,
    document_library_target: DocumentLibraryTarget | None = None,
) -> tuple[list[RegisteredTool], dict[str, RepositoryBindingRef]]:
    """Blueprint の resource_requirements と選択済み source から最小 Tool 集合を解決する。

    業務固有 capability には依存せず、blueprint の宣言順に要求を走査する。必須要求が未選択
    なら fail closed とし、直接公開する Tool は必ず permission snapshot の
    allowed_capabilities に含まれていなければならない。文書保存先は検証済み binding と
    提案権限だけを要求し、apply capability を Agent 権限へ追加しない。

    併せて repository 種別の凍結 binding を requirement_key ごとに返す。物化 (計画 §19 W4) は
    この検証済み結果だけを入力とし、Run snapshot を再解釈しない。
    """

    tools: list[RegisteredTool] = []
    bound_tool_sources: dict[str, list[RegisteredTool]] = {}
    repository_bindings: dict[str, RepositoryBindingRef] = {}
    blueprint = resolve_skill_definition(manifest)
    requirements = (
        _sequence(blueprint.get("resource_requirements")) if isinstance(blueprint, Mapping) else []
    )
    required_keys = (
        required_resource_keys(blueprint) if isinstance(blueprint, Mapping) else frozenset()
    )
    requirement_keys = {
        item.get("key") for item in requirements if isinstance(item, Mapping)
    }
    if any(
        key not in requirement_keys and is_document_library_source(value)
        for key, value in claimed_run.selected_sources_json.items()
    ):
        raise ValueError("Document library selection has no resource requirement")
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        selected_source = _selected_source(
            requirement, claimed_run, document_library_target=document_library_target
        )
        if selected_source is None:
            if requirement.get("key") in required_keys:
                raise ValueError("Required data source has no selected provider")
            continue
        capability, provider, integration_id, binding_id = selected_source
        if capability == DOCUMENT_WRITE_CAPABILITY:
            # 保存先には読取 Tool がない。原 slot を検証しても直接 write 権限は渡さない。
            if CHANGE_PROPOSE_CAPABILITY not in allowed:
                raise ValueError("Document library requires proposal permission")
            continue
        if capability not in allowed:
            raise ValueError("Resolved Tool is not allowed by the permission snapshot")
        if capability in EFFECT_CAPABILITIES:
            raise ValueError("Write capability cannot be exposed as a direct Agent Tool")
        requirement_key = requirement.get("key")
        if (
            requirement.get("kind") == "repository"
            and isinstance(requirement_key, str)
            and integration_id is not None
            and binding_id is not None
        ):
            repository_bindings[requirement_key] = RepositoryBindingRef(
                provider=provider,
                integration_id=integration_id,
                binding_id=binding_id,
            )
        resolved = registry.resolve(
            capability,
            provider=provider,
            integration_id=integration_id,
            binding_id=binding_id,
            resource_key=str(requirement_key) if integration_id is not None and binding_id is not None else None,
            execution_profile=execution_profile,
        )
        if capability in DOCUMENT_CAPABILITIES and any(
            tool.capability == capability for tool in tools
        ):
            # 複数の文書 slot は同じ Run 集合を読む。SDK 名を重複登録しない。
            continue
        tools.append(resolved)
        if integration_id is not None and binding_id is not None:
            # 一つの資源が複数の読取能力を宣言できる。代表能力だけでなく、後続 Tool の
            # 解決にも検証済みの原 binding を渡す。Provider 名による別 slot の推測は禁止する。
            for declared in _sequence(requirement.get("capabilities")):
                if (
                    not isinstance(declared, str)
                    or declared not in allowed
                    or declared in EFFECT_CAPABILITIES
                ):
                    continue
                sources = bound_tool_sources.setdefault(declared, [])
                if all(item.resource_key != resolved.resource_key for item in sources):
                    sources.append(resolved)
    if uses_modern_runtime(claimed_run.task_snapshot_json):
        for original in tuple(tools):
            if original.capability != "database.read/v1":
                continue
            for capability in ("database.read/v2", "database.describe/v1"):
                if capability not in allowed:
                    raise ValueError("Database assistance requires frozen permission")
                tools.append(registry.resolve(
                    capability, provider=original.provider,
                    integration_id=original.integration_id, binding_id=original.binding_id,
                    resource_key=original.resource_key,
                    execution_profile=execution_profile,
                ))
            tools.remove(original)
    resolved_capabilities = {tool.capability for tool in tools}
    if "database.read/v2" in resolved_capabilities:
        resolved_capabilities.add("database.read/v1")
    for tool_requirement in _sequence(manifest.get("tools")):
        if not isinstance(tool_requirement, Mapping):
            continue
        tool_capability = tool_requirement.get("capability")
        if not isinstance(tool_capability, str):
            continue
        if tool_capability in bound_tool_sources and tool_capability in allowed:
            for source in bound_tool_sources[tool_capability]:
                equivalent = {tool_capability}
                if tool_capability == "database.read/v1" and uses_modern_runtime(claimed_run.task_snapshot_json):
                    equivalent.add("database.read/v2")
                if any(t.capability in equivalent and t.resource_key == source.resource_key for t in tools):
                    continue
                try:
                    tools.append(registry.resolve(
                        tool_capability, provider=source.provider,
                        integration_id=source.integration_id, binding_id=source.binding_id,
                        resource_key=source.resource_key, execution_profile=execution_profile,
                    ))
                except LookupError:
                    if bool(tool_requirement.get("required", False)):
                        raise
            resolved_capabilities.update(t.capability for t in tools)
            continue
        if tool_capability in resolved_capabilities:
            continue
        if tool_capability not in allowed:
            if bool(tool_requirement.get("required", False)):
                raise ValueError("Resolved Tool is not allowed by the permission snapshot")
            continue
        # effect 宣言は別 registry/批准で処理し、必須指定でも直接 Tool に昇格させない。
        if tool_capability in EFFECT_CAPABILITIES:
            continue
        try:
            if tool_capability in DOCUMENT_CAPABILITIES:
                if not any(tool.capability in DOCUMENT_CAPABILITIES for tool in tools):
                    raise LookupError("Document Tool requires a frozen document selection")
                resolved = registry.resolve(
                    tool_capability, provider=DOCUMENT_PROVIDER, integration_id=None,
                    execution_profile=execution_profile,
                )
            else:
                resolved = registry.resolve_unbound(
                    tool_capability,
                    execution_profile=execution_profile,
                )
        except LookupError:
            if bool(tool_requirement.get("required", False)):
                raise
            continue
        tools.append(resolved)
        resolved_capabilities.add(tool_capability)
    if (
        INTERACTION_REQUEST_CAPABILITY in allowed
        and INTERACTION_REQUEST_CAPABILITY not in resolved_capabilities
    ):
        tools.append(
            registry.resolve_unbound(
                INTERACTION_REQUEST_CAPABILITY,
                execution_profile=execution_profile,
            )
        )
    if (
        CHANGE_PROPOSE_CAPABILITY in allowed
        and CHANGE_PROPOSE_CAPABILITY not in resolved_capabilities
    ):
        tools.append(
            registry.resolve_unbound(
                CHANGE_PROPOSE_CAPABILITY,
                execution_profile=execution_profile,
            )
        )
    for auxiliary in ("audit.export/v1", "tool.sequence/v1", "artifact.append/v1"):
        if auxiliary in allowed and auxiliary not in resolved_capabilities:
            tools.append(registry.resolve_unbound(auxiliary, execution_profile=execution_profile))
    return tools, repository_bindings


def _selected_source(
    requirement: Mapping[str, Any], claimed_run: ClaimedRun,
    *, document_library_target: DocumentLibraryTarget | None = None,
) -> tuple[str, str, UUID | None, UUID | None] | None:
    """構造化 snapshot を再検証し、capability・provider と frozen Integration identity を返す。

    要求宣言は blueprint へ一本化したため、照合 key は requirement の `key` そのものとする。
    以前は capability 一致で走査していたが、同 capability を共有する要求同士で取り違える
    余地があり、宣言側と選択側で key がずれると解決不能になっていた。
    """

    requirement_key = requirement.get("key")
    if not isinstance(requirement_key, str):
        return None
    value = claimed_run.selected_sources_json.get(requirement_key)
    if not isinstance(value, Mapping):
        return None
    capability = value.get("capability")
    if not isinstance(capability, str):
        raise ValueError("Selected data source capability is invalid")
    declared = frozenset(
        item for item in _sequence(requirement.get("capabilities")) if isinstance(item, str)
    )
    if capability not in declared:
        # 凍結 snapshot が要求宣言外の capability を指すのは改竄か不整合。fail closed。
        raise ValueError("Selected capability is not declared by the resource requirement")
    provider = value.get("provider")
    if not isinstance(provider, str):
        raise ValueError("Selected data source provider is invalid")
    if is_document_library_source(value):
        if (
            requirement.get("kind") != "document"
            or requirement.get("access") != "write"
            or declared != {DOCUMENT_WRITE_CAPABILITY}
            or document_library_target is None
        ):
            raise ValueError("Selected document library is unavailable or invalid")
        frozen = parse_document_library_source(
            value, project_id=claimed_run.project_id, run_id=claimed_run.run_id,
            requirement_key=requirement_key,
        )
        if frozen.target != document_library_target:
            raise ValueError("Document library changed after Run creation")
        # 旧版は原結果の核対だけを許可し、完了できない保存 Run のモデル起動前に拒否する。
        if frozen.revision != DOCUMENT_LIBRARY_REVISION:
            raise ValueError("Legacy document library binding is read-only")
        return capability, provider, None, frozen.binding_id
    if requirement.get("kind") == "document":
        if (
            capability not in DOCUMENT_CAPABILITIES
            or provider != DOCUMENT_PROVIDER
            or requirement.get("access", "read") != "read"
        ):
            raise ValueError("Selected document source is invalid")
        selected_document_snapshots({requirement_key: value}, project_id=claimed_run.project_id)
        if "document_library" in value:
            reference = parse_document_library_reference(
                value["document_library"], project_id=claimed_run.project_id
            )
            if (
                document_library_target is None
                or reference != document_library_target.reference(claimed_run.project_id)
            ):
                raise ValueError("Input document library changed after Run creation")
        return capability, provider, None, None
    raw_integration_id = value.get("integration_id")
    if raw_integration_id is None:
        raise ValueError("Selected data source requires a frozen Integration binding")
    try:
        integration_id = UUID(str(raw_integration_id))
    except ValueError as error:
        raise ValueError("Selected Integration identity is invalid") from error
    binding_id = _validate_run_binding_snapshot(
        claimed_run,
        requirement_key=requirement_key,
        value=value,
        integration_id=integration_id,
        provider=provider,
    )
    return capability, provider, integration_id, binding_id


def _validate_run_binding_snapshot(
    claimed_run: ClaimedRun,
    *,
    requirement_key: str,
    value: Mapping[str, Any],
    integration_id: UUID,
    provider: str,
) -> UUID:
    """Run source の permission-sensitive fields と checksum の一致を検証する。"""

    revision = value.get("revision")
    scope = value.get("scope")
    resource_kind = value.get("resource_kind")
    binding_capability = value.get("binding_capability")
    checksum = value.get("binding_checksum")
    binding_id = value.get("binding_id")
    if (
        not isinstance(revision, str)
        or not isinstance(scope, dict)
        or not isinstance(resource_kind, str)
        or not isinstance(binding_capability, str)
        or not isinstance(checksum, str)
        or not isinstance(binding_id, str)
    ):
        raise ValueError("Run ResourceBinding snapshot is incomplete")
    expected = binding_checksum(
        project_id=claimed_run.project_id,
        scope_level=ResourceBindingLevel.RUN,
        scope_key=str(claimed_run.run_id),
        requirement_key=requirement_key,
        resource_kind=resource_kind,
        integration_id=integration_id,
        provider=provider,
        capability_version=binding_capability,
        revision=revision,
        scope=scope,
    )
    if checksum != expected:
        raise ValueError("Run ResourceBinding checksum does not match frozen content")
    try:
        return UUID(binding_id)
    except ValueError as error:
        raise ValueError("Run ResourceBinding identity is invalid") from error


def _sequence(value: Any) -> Sequence[Any]:
    """List 状の値だけを列挙対象として返し、str/bytes を除外する。"""

    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    return ()


def _validate_actor(permission: Mapping[str, Any], actor_id: UUID) -> None:
    """Permission snapshot に固定した actor と claim identity の一致を保証する。"""

    try:
        snapshot_actor = UUID(str(permission.get("actor_id")))
    except ValueError as error:
        raise ValueError("Permission snapshot actor is invalid") from error
    if snapshot_actor != actor_id:
        raise ValueError("Permission snapshot actor does not match the claimed Run")


def _required_string(value: Mapping[str, Any], key: str) -> str:
    """Task snapshot の必須 string 参照を返す。"""

    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Task snapshot is missing {key}")
    return result


def _positive_int(value: Mapping[str, Any], key: str) -> int:
    """Resource limit の bool や無制限値を拒否する。"""

    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
        raise ValueError(f"Run limit is invalid: {key}")
    return result


def _optional_positive_number(value: Mapping[str, Any], key: str) -> float | None:
    """Optional budget を正の finite float へ制限する。"""

    result = value.get(key)
    if result is None:
        return None
    if isinstance(result, bool) or not isinstance(result, int | float):
        raise ValueError(f"Run limit is invalid: {key}")
    number = float(result)
    if number <= 0 or number == float("inf") or number != number:
        raise ValueError(f"Run limit is invalid: {key}")
    return number
