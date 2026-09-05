"""凍結済み Run snapshot から AgentTaskBrief を決定的に組み立てる。

docs/11 §7 のとおり、Worker へ渡すのは要約ではなく Skill guidance の全量である。Brief は
「目標・必須規則・品質基準・禁止事項・資源・許可 Tool・効果方針・上限」を一つの検査可能な
document へ固定し、canonical checksum で監査対象にする。ここは指示の組成だけを担当し、
権限判断は行わない。Tool の可否と effect の可否は permission snapshot と platform policy が
独立して決めるため、Skill が Brief 経由で権限を広げることはできない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from projectmind.agent.domain import MaterializedResource, RegisteredTool, RunLimits
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.skills.capability_blueprint import resolve_capability_blueprint

AGENT_TASK_BRIEF_VERSION = "projectmind.agent-task-brief/v1"

_GUIDANCE_SECTIONS = (
    "required_rules",
    "recommended_steps",
    "quality_criteria",
    "prohibited_actions",
)


class ExecutionProfile(StrEnum):
    """Agent に許す自律度。値の並びが自律度の強さそのものを表す。"""

    GUIDED = "GUIDED"
    SUPERVISED = "SUPERVISED"
    DELEGATED = "DELEGATED"


class ProfileSource(StrEnum):
    """解決済み profile が誰の判断に由来するかの監査値。"""

    PLATFORM_DEFAULT = "platform_default"
    SKILL_RECOMMENDATION = "skill_recommendation"
    PLATFORM_MAXIMUM = "platform_maximum"


# 自律度の全順序。profile 名の比較ではなく、この順序だけで上限判定を行う。
_AUTONOMY_ORDER = (ExecutionProfile.GUIDED, ExecutionProfile.SUPERVISED, ExecutionProfile.DELEGATED)

# docs/01 §15 / docs/11 §7.2 の既定。Skill 側の宣言が無いときはこれを使う。
PLATFORM_DEFAULT_PROFILE = ExecutionProfile.SUPERVISED

# DELEGATED は「ADMIN が事前許可した資源と低 risk 効果」でのみ許される (docs/11 §7.2)。
# 事前許可 (S5) が未実装である以上、Skill が DELEGATED を推奨しても platform 上限で抑える。
PLATFORM_MAXIMUM_PROFILE = ExecutionProfile.SUPERVISED

_PROFILE_INSTRUCTIONS = {
    ExecutionProfile.GUIDED: (
        "Execution profile GUIDED: follow the recommended steps in the given order and treat "
        "them as strong guidance. Report rather than improvise when a step cannot be applied."
    ),
    ExecutionProfile.SUPERVISED: (
        "Execution profile SUPERVISED: plan the read-only analysis yourself and adapt the "
        "recommended steps whenever the objective is better served. The required rules and "
        "prohibited actions still bind you. Stop and report instead of guessing when a resource "
        "is ambiguous or a business judgement is needed."
    ),
    ExecutionProfile.DELEGATED: (
        "Execution profile DELEGATED: plan and act within the pre-authorised resources and "
        "low-risk effects only. Anything outside that scope still requires approval."
    ),
}


@dataclass(frozen=True, slots=True)
class ResolvedExecutionProfile:
    """合成後の profile と、その値を誰が決めたかの由来。"""

    profile: ExecutionProfile
    source: ProfileSource


@dataclass(frozen=True, slots=True)
class CompiledAgentTaskBrief:
    """正規化済み Brief と canonical checksum の不変な組。"""

    brief: dict[str, Any]
    checksum: str


def resolve_execution_profile(
    blueprint: Mapping[str, Any],
    *,
    platform_maximum: ExecutionProfile = PLATFORM_MAXIMUM_PROFILE,
) -> ResolvedExecutionProfile:
    """platform 既定と Skill 推奨を合成し、platform 上限で抑えた profile を返す。

    Skill の推奨は要求であって権限ではない。より保守的な推奨 (GUIDED) はそのまま採用し、
    上限を超える推奨は黙って降格させたことが監査で分かるよう `platform_maximum` を由来に
    残す。docs/11 §4 のとおり全域 bypass は提供しない。
    """

    recommended = _profile_or_none(
        _mapping(blueprint, "execution_preferences").get("recommended_profile")
    )
    if recommended is None:
        return ResolvedExecutionProfile(PLATFORM_DEFAULT_PROFILE, ProfileSource.PLATFORM_DEFAULT)
    if _AUTONOMY_ORDER.index(recommended) > _AUTONOMY_ORDER.index(platform_maximum):
        return ResolvedExecutionProfile(platform_maximum, ProfileSource.PLATFORM_MAXIMUM)
    return ResolvedExecutionProfile(recommended, ProfileSource.SKILL_RECOMMENDATION)


def build_agent_task_brief(
    *,
    run_id: UUID,
    task_snapshot: Mapping[str, Any],
    manifest: Mapping[str, Any],
    selected_sources: Mapping[str, Any],
    tools: Sequence[RegisteredTool],
    limits: RunLimits,
    segment_no: int = 1,
    segment_objective: str | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    materialized: Sequence[MaterializedResource] = (),
) -> CompiledAgentTaskBrief:
    """凍結済み Run snapshot から Brief を組み立て、canonical checksum を付けて返す。

    入力はすべて Run 作成時に凍結され、呼び出し側で checksum 照合済みの値である。組成は
    決定的なので、同じ Run の同じ Attempt/Segment からは常に同じ checksum が得られる。
    """

    blueprint = resolve_capability_blueprint(manifest)
    if blueprint is None:
        # Run が凍結した manifest は publish gate 通過済みであり、必ず蓝图を持つ。無いまま
        # 実行すると Skill の必須規則も目標も渡らないまま Agent が走るため、閉じて失敗させる。
        raise ValueError("Run SkillVersion Manifest does not declare a CapabilityBlueprint")
    task_key = _string(task_snapshot.get("task_key"))
    capability = _string(task_snapshot.get("capability"))
    blueprint_task = _find_blueprint_task(blueprint, task_key=task_key, capability=capability)
    objective = _objective_text(blueprint_task, manifest=manifest, capability=capability)
    profile = resolve_execution_profile(blueprint)
    preferences = _mapping(blueprint, "execution_preferences")
    guidance = _mapping(blueprint, "guidance")
    brief: dict[str, Any] = {
        "brief_version": AGENT_TASK_BRIEF_VERSION,
        "identity": {
            "run_id": str(run_id),
            "segment_no": segment_no,
            "task_key": task_key,
            "capability": capability,
            "skill_version_id": _string(task_snapshot.get("skill_version_id")),
            "manifest_checksum": _string(task_snapshot.get("manifest_checksum")),
            "blueprint_checksum": f"sha256:{sha256_hex(canonical_json(blueprint))}",
            "result_schema_checksum": _string(task_snapshot.get("output_schema_checksum"))
            or _string(task_snapshot.get("output_schema")),
        },
        "objective": {
            # S3 は単一 Segment なので Run 総目標と Segment 目標が一致する。S4 で Segment
            # ごとの目標が分岐しても、Agent が Run 全体の目標を見失わないよう両方を残す。
            "run_objective": objective,
            "segment_objective": segment_objective or objective,
            "success_criteria": _note_list(blueprint_task.get("success_criteria")),
        },
        "guidance": {name: _note_list(guidance.get(name)) for name in _GUIDANCE_SECTIONS},
        "execution": {
            "profile": profile.profile.value,
            "profile_source": profile.source.value,
            "stop_conditions": _note_list(preferences.get("stop_conditions")),
            "session_split_hints": _note_list(preferences.get("session_split_hints")),
        },
        "resources": _resources(
            blueprint, selected_sources=selected_sources, materialized=materialized
        ),
        "allowed_tools": [
            {
                "capability": tool.capability,
                "provider": tool.provider,
                "read_only": tool.read_only,
            }
            for tool in tools
        ],
        "effect_policy": _effect_policy(blueprint, manifest=manifest),
        "interaction_policy": _interaction_policy(blueprint),
        "checkpoint": _checkpoint(checkpoint),
        "deliverables": _deliverables(blueprint_task),
        "limits": {
            "max_turns": limits.max_turns,
            "wall_timeout_seconds": limits.wall_timeout_seconds,
            "max_output_bytes": limits.max_output_bytes,
            "max_budget_usd": limits.max_budget_usd,
        },
    }
    return CompiledAgentTaskBrief(
        brief=brief,
        checksum=f"sha256:{sha256_hex(canonical_json(brief))}",
    )


def render_task_brief_prompt(
    brief: Mapping[str, Any],
    *,
    input_json: Mapping[str, Any],
    output_schema: Mapping[str, Any],
) -> str:
    """Brief を SDK 非依存の実行 prompt へ描画する。

    docs/11 §7.1 の「要約だけを送って required rule を落とさない」を満たすため、必須規則・
    禁止事項・品質基準・停止条件は省略も要約もせずそのまま並べる。推奨手順は profile 文で
    強制度を明示し、SUPERVISED では Agent が組み替えられることを伝える。業務入力は検証済み
    JSON として同梱し、来源本文は複製しない。
    """

    sections = [
        f"Objective: {brief['objective']['segment_objective']}",
        _PROFILE_INSTRUCTIONS[ExecutionProfile(brief["execution"]["profile"])],
    ]
    guidance = brief["guidance"]
    _append_notes(
        sections,
        "Rules you MUST follow (declared by the Skill source; never skip or reinterpret them)",
        guidance["required_rules"],
    )
    _append_notes(
        sections,
        "You MUST NOT do any of the following",
        guidance["prohibited_actions"],
    )
    _append_notes(sections, "Success criteria", brief["objective"]["success_criteria"])
    _append_notes(sections, "Quality criteria", guidance["quality_criteria"])
    _append_notes(sections, "Recommended steps", guidance["recommended_steps"])
    _append_notes(sections, "Stop and report when", brief["execution"]["stop_conditions"])
    _append_notes(sections, "Expected deliverables", brief["deliverables"], key="description")
    _append_materialization(sections, brief["resources"])
    sections.append(_tool_instruction(brief["allowed_tools"]))
    sections.append(_effect_instruction(brief["effect_policy"]))
    sections.append(_interaction_instruction(brief["interaction_policy"]))
    checkpoint = brief["checkpoint"]
    if checkpoint["summary"] is not None or checkpoint["user_responses"]:
        sections.append(
            f"Audited checkpoint from prior segments (JSON): {canonical_json(checkpoint)}"
        )
    sections.append(f"Task input (JSON): {canonical_json(input_json)}")
    # 非公式 Anthropic 互換 endpoint が API-level output_format を無視しても、同じ Schema を
    # prompt に固定し、後段で決定的に JSON parse/validation できるようにする。
    sections.append(
        "Return ONLY one JSON object matching the exact schema below. "
        "Do not use Markdown, code fences, commentary, or text outside the JSON object. "
        f"Output JSON Schema: {canonical_json(output_schema)}"
    )
    return "\n\n".join(sections)


def _append_notes(
    sections: list[str], title: str, notes: Sequence[Any], *, key: str = "text"
) -> None:
    """空でない note 群だけを箇条書き section として追加する。"""

    lines = [f"- {item[key]}" for item in notes if isinstance(item, Mapping) and item.get(key)]
    if lines:
        sections.append("\n".join([f"{title}:", *lines]))


def _append_materialization(sections: list[str], resources: Sequence[Any]) -> None:
    """物化済み資源の落点を prompt へ明示する (計画 §19 W6)。

    §19 は「Agent が既存の workspace.search/read で自走発見する」ことを前提にしているが、
    どこに置いたかを伝えなければ、その発見は模型が自力で `input/` を思いつくかどうかに懸かる。
    失敗時は「資源が無い」という報告になり、本当に未束縛な場合と区別できない。索引と clean up
    済み清单の位置、`skipped` の意味まで含めて明示する。
    """

    placed = [
        (resource, resource["materialization"])
        for resource in resources
        if isinstance(resource, Mapping) and isinstance(resource.get("materialization"), Mapping)
    ]
    if not placed:
        return
    lines = [
        "Materialized resources (already on disk in this Run; read them with the "
        "workspace.search/v1 and workspace.read/v1 tools):"
    ]
    for resource, placement in placed:
        details = [f"files under {placement['root']}/"]
        revision = placement.get("revision")
        if revision:
            details.append(f"frozen revision {revision}")
        details.append(f"file index {placement['index']}")
        details.append(f"manifest {placement['manifest']}")
        history = placement.get("history")
        if history:
            details.append(f"commit history {history}")
        lines.append(
            f"- {resource['key']} ({resource['kind']}): "
            + "; ".join(details)
            + f" [{placement['materialized_files']} files, "
            f"{placement['skipped_files']} skipped]"
        )
    lines.append(
        "Start from the file index or the manifest before searching, and quote the manifest "
        "revision when you cite a file. Paths listed in the manifest \"skipped\" array exist "
        "but could not be read by the platform (binary, oversized, or unconvertible); never "
        "report them as missing."
    )
    sections.append("\n".join(lines))


def _tool_instruction(allowed_tools: Sequence[Any]) -> str:
    """許可 Tool と Evidence 引用義務を伝える指示文を返す。"""

    capabilities = [
        item["capability"]
        for item in allowed_tools
        if isinstance(item, Mapping) and isinstance(item.get("capability"), str)
    ]
    if not capabilities:
        return (
            "No external data-source tools are available; rely only on the provided input and "
            "do not request tools."
        )
    return (
        f"Use the available read-only tools ({', '.join(capabilities)}) to gather Evidence "
        "before drawing conclusions. Every factual conclusion must cite Evidence references "
        "returned by tools."
    )


def _effect_instruction(effect_policy: Mapping[str, Any]) -> str:
    """効果意図と実際の権限の差を Agent へ明示する指示文を返す。

    Skill が apply を宣言していても、実行可否を決めるのは platform policy である
    (docs/01 §15.1「effect intent 不等于 permission」)。宣言を伝えないと Agent は書き込みを
    試み、伝えるだけで方針を書かないと宣言が許可と読める。両方を同じ文で固定する。
    """

    declared = [
        item
        for item in effect_policy.get("declared_intents", [])
        if isinstance(item, Mapping) and item.get("mode") in {"propose", "apply"}
    ]
    base = (
        "External writes are denied for this Run. Do not attempt to modify any external system."
    )
    if not declared:
        return base
    propose_operations = ", ".join(
        str(item.get("operation"))
        for item in declared
        if item.get("mode") == "propose" and item.get("operation")
    )
    apply_operations = ", ".join(
        str(item.get("operation"))
        for item in declared
        if item.get("mode") == "apply" and item.get("operation")
    )
    instructions = [
        base,
        "Declaring an effect intent is never a permission or an approval.",
    ]
    if propose_operations:
        instructions.append(
            "For propose-only intents, create the patch/commit plan as an Outcome deliverable "
            f"and never apply it ({propose_operations})."
        )
    if apply_operations:
        instructions.append(
            "After gathering Evidence for an apply intent, call change.propose/v1 with the exact "
            "frozen target, revision, changes, and checkpoint; ProjectMind will independently "
            f"validate and request approval ({apply_operations})."
        )
    return " ".join(instructions)


def _interaction_instruction(interaction_policy: Sequence[Any]) -> str:
    """構造化 interaction Tool の停止条件を Agent へ明示する。"""

    declared = [item for item in interaction_policy if isinstance(item, Mapping)]
    declared_text = canonical_json(declared)
    return (
        "When a required fact, business choice, review, or approval is needed, do not guess and "
        "do not finish with a BLOCKED Outcome. Call interaction.request/v1 exactly once with a "
        "public question and an auditable checkpoint; ProjectMind will pause this session and "
        "resume a later Segment after the user responds. Declared interaction points: "
        f"{declared_text}"
    )


def _find_blueprint_task(
    blueprint: Mapping[str, Any], *, task_key: str, capability: str
) -> Mapping[str, Any]:
    """Run の task に対応する蓝图 task を key、無ければ capability で引く。

    manifest と蓝图の task key 一致は publish gate で強制していない。取りこぼしても
    guidance は蓝图直下にあるため必須規則は落ちないが、目標と成功条件は task 側にある。
    capability での再照合まで試し、それでも無ければ manifest 由来の目標へ退避する。
    """

    tasks = _object_list(blueprint.get("tasks"))
    for task in tasks:
        if task_key and task.get("key") == task_key:
            return task
    for task in tasks:
        if capability and task.get("capability") == capability:
            return task
    return {}


def _objective_text(
    blueprint_task: Mapping[str, Any], *, manifest: Mapping[str, Any], capability: str
) -> str:
    """蓝图の目標、無ければ manifest の capability title から目標文を決める。"""

    objective = _string(blueprint_task.get("objective"))
    if objective:
        return objective
    title = _capability_title(manifest, capability) or capability or "the requested task"
    return f"Perform the task '{title}'."


def _capability_title(manifest: Mapping[str, Any], capability: str) -> str:
    """Manifest.capabilities から capability の表示 title を引く (無ければ空文字)。"""

    for item in _object_list(manifest.get("capabilities")):
        if item.get("key") == capability:
            return _string(item.get("title"))
    return ""


def _resources(
    blueprint: Mapping[str, Any],
    *,
    selected_sources: Mapping[str, Any],
    materialized: Sequence[MaterializedResource] = (),
) -> list[dict[str, Any]]:
    """資源要求へ Run が凍結した provider 選択を突き合わせて返す。

    束縛は capability 単位で照合する。S2 の Run 級 ResourceBinding が未実装のため、現状の
    正本は manifest data_source から作られた `selected_sources` である。要求と Run 選択の
    どちらか一方しか無い状態を隠さないよう、未束縛は `binding: null` として明示する。
    """

    bound = {
        capability: provider
        for value in selected_sources.values()
        if isinstance(value, Mapping)
        and isinstance(capability := value.get("capability"), str)
        and isinstance(provider := value.get("provider"), str)
    }
    resources: list[dict[str, Any]] = []
    for requirement in _object_list(blueprint.get("resource_requirements")):
        key = _string(requirement.get("key"))
        if not key:
            continue
        hints = _string_list(requirement.get("capabilities"))
        resource: dict[str, Any] = {
            "key": key,
            "kind": _string(requirement.get("kind")),
            "required": requirement.get("required") is True,
            "access": _string(requirement.get("access")),
            "capabilities": hints,
            "binding": next(
                (
                    {
                        "capability": hint,
                        "provider": bound[hint],
                        "binding_source": "RUN_PREFLIGHT",
                    }
                    for hint in hints
                    if hint in bound
                ),
                None,
            ),
        }
        guidance = _string(requirement.get("selection_guidance"))
        if guidance:
            resource["selection_guidance"] = guidance
        placement = _materialization(resource["kind"], key, materialized)
        if placement is not None:
            resource["materialization"] = placement
        resources.append(resource)
    return resources


def _materialization(
    kind: str, key: str, materialized: Sequence[MaterializedResource]
) -> dict[str, Any] | None:
    """当該要求に対応する物化落点を返す。物化していなければ None。

    document は D-W3=(a) により Project 単位で一箇所へ集約するため、要求 key ではなく種別で
    対応付ける (document 要求が複数あっても実体は同じ `input/documents/` 一つ)。repository は
    要求ごとに根が分かれるため key で対応付ける。
    """

    for resource in materialized:
        matched = (
            resource.kind == "document"
            if kind == "document"
            else resource.kind == kind and resource.requirement_key == key
        )
        if not matched:
            continue
        placement: dict[str, Any] = {
            "root": resource.root,
            "manifest": resource.manifest_path,
            "index": resource.index_path,
            "materialized_files": resource.files,
            "skipped_files": resource.skipped,
        }
        if resource.history_path is not None:
            placement["history"] = resource.history_path
        if resource.revision is not None:
            placement["revision"] = resource.revision
        return placement
    return None


def _effect_policy(
    blueprint: Mapping[str, Any], *, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Skill が宣言した効果意図と、Run に適用される platform 方針を並べて固定する。

    `executable` は「今この Run で意図を安全に遂行できるか」であり、Skill の宣言ではない。
    observe/propose は外部 write を伴わないため実行可能、apply は登録 Provider と別承認が
    揃うまで実行不可とする。apply の candidate 作成だけは platform control Tool が担う。
    """

    permissions = _mapping(manifest, "permissions")
    intents: list[dict[str, Any]] = []
    for intent in _object_list(blueprint.get("effect_intents")):
        key = _string(intent.get("key"))
        if not key:
            continue
        mode = _string(intent.get("mode"))
        entry: dict[str, Any] = {
            "key": key,
            "mode": mode,
            "operation": _string(intent.get("operation")),
            "risk": _string(intent.get("risk")),
            "executable": mode in {"observe", "propose"},
        }
        resource_key = _string(intent.get("resource_key"))
        if resource_key:
            entry["resource_key"] = resource_key
        intents.append(entry)
    return {
        "external_write": _string(permissions.get("external_write_policy")) or "deny",
        "network_scope": _string(permissions.get("network_scope"))
        or "project_integrations_only",
        "declared_intents": intents,
    }


def _deliverables(blueprint_task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """蓝图 task の交付物宣言を Brief 用に写す。"""

    return [
        {
            "key": _string(item.get("key")),
            "kind": _string(item.get("kind")),
            "description": _string(item.get("description")),
        }
        for item in _object_list(blueprint_task.get("deliverables"))
        if _string(item.get("key"))
    ]


def _interaction_policy(blueprint: Mapping[str, Any]) -> list[dict[str, Any]]:
    """蓝图の interaction point を省略せず Brief の公開 policy へ固定する。"""

    interactions: list[dict[str, Any]] = []
    for item in _object_list(blueprint.get("interaction_points")):
        key = _string(item.get("key"))
        interaction_type = _string(item.get("type"))
        condition = _string(item.get("condition"))
        if not key or not interaction_type or not condition:
            continue
        interaction = {
            "key": key,
            "type": interaction_type,
            "condition": condition,
        }
        prompt = _string(item.get("prompt"))
        if prompt:
            interaction["prompt"] = prompt
        interactions.append(interaction)
    return interactions


def _checkpoint(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """前 Segment の公開 checkpoint を Brief contract の安定 shape へ正規化する。"""

    source = value or {}
    summary = source.get("summary")
    return {
        "summary": summary if isinstance(summary, str) and summary else None,
        "confirmed_facts": _string_list(source.get("confirmed_facts")),
        "user_responses": [
            dict(item) for item in _object_list(source.get("user_responses"))
        ],
        "evidence_refs": _string_list(source.get("evidence_refs")),
        "artifact_refs": _string_list(source.get("artifact_refs")),
        "change_proposal_refs": _string_list(source.get("change_proposal_refs")),
    }


def _note_list(value: Any) -> list[dict[str, Any]]:
    """key/text を持つ note だけを安定形へ写す。"""

    return [
        {"key": _string(item.get("key")), "text": _string(item.get("text"))}
        for item in _object_list(value)
        if _string(item.get("key")) and _string(item.get("text"))
    ]


def _profile_or_none(value: Any) -> ExecutionProfile | None:
    """既知の profile 名だけを列挙値へ変換する。"""

    return ExecutionProfile(value) if value in set(ExecutionProfile) else None


def _mapping(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Object field を空 object fallback 付きで返す。"""

    item = value.get(key)
    return cast(dict[str, Any], item) if isinstance(item, dict) else {}


def _object_list(value: Any) -> list[dict[str, Any]]:
    """Array から object item だけを返す。"""

    return (
        [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _string_list(value: Any) -> list[str]:
    """Array から string item だけを返す。"""

    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _string(value: Any) -> str:
    """空文字を除いた string、または空文字を返す。"""

    return value if isinstance(value, str) and value else ""
