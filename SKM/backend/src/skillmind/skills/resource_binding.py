"""CapabilityBlueprint の資源要求を Project 側の候補資源へ照合し、就緒度を決定する。

同じ SkillVersion でも Project ごとに束縛できる資源は異なる。ここは「発行可否」ではなく
「今この Project で実行できるか」を判定する層であり、docs/11 §6 の就緒度語彙を単一実装で
提供する。権限付与や Integration scope の拡大は行わない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID


class TaskReadinessLevel(StrEnum):
    """Task が今この Project で到達できる実行段階。"""

    GUIDANCE_ONLY = "GUIDANCE_ONLY"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    RUNNABLE = "RUNNABLE"
    ACTIONABLE = "ACTIONABLE"


class RequirementStatus(StrEnum):
    """一つの資源要求が今どこで詰まっているかの分類。"""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ProjectResourceCandidate:
    """Project 内で資源要求へ束縛できる一つの具体資源。

    Secret や接続情報は持たない。Workspace が候補として提示し、preflight で選択するための
    識別子と能力だけを運ぶ。
    """

    key: str
    kind: str
    provider: str
    label: str
    capabilities: tuple[str, ...]
    integration_id: UUID | None = None
    revision: str | None = None
    scope: Mapping[str, Any] | None = None


class ProjectResourceCatalog(Protocol):
    """Project が束縛可能な資源候補を列挙する read-only port。"""

    async def candidates(self, *, project_id: UUID) -> tuple[ProjectResourceCandidate, ...]:
        """当該 Project で利用できる資源候補を返す。越権分は含めない。"""

        ...


@dataclass(frozen=True, slots=True)
class RequirementBinding:
    """一つの資源要求に対する候補と不足理由の逐項状態。"""

    key: str
    kind: str
    required: bool
    access: str
    status: RequirementStatus
    reason: str
    candidates: tuple[ProjectResourceCandidate, ...]
    capabilities: tuple[str, ...]
    selection_guidance: str | None


@dataclass(frozen=True, slots=True)
class TaskReadiness:
    """Task 単位の就緒度と、その判断根拠となる逐項束縛状態。"""

    level: TaskReadinessLevel
    requirements: tuple[RequirementBinding, ...]


def evaluate_blueprint_readiness(
    blueprint: Mapping[str, Any],
    *,
    candidates: Sequence[ProjectResourceCandidate],
    registered_capabilities: frozenset[str],
    registered_write_capabilities: frozenset[str] = frozenset(),
    installed_provider_capabilities: Mapping[str, frozenset[str]] | None = None,
) -> TaskReadiness:
    """蓝图の資源要求と Project 候補から逐項束縛状態と就緒度を決定する。

    ここでは具体 instance の選択までは行わない。docs/05 §7.2 のとおり就緒度は「必要な資源と
    Tool が揃っているか」の判定であり、どの候補を使うかは Task 設定・preflight・実行中の
    CHOICE で確定する。候補が複数あることは不足ではない。

    ``installed_provider_capabilities`` は能力→実装配線済み Provider 名 (候補と同じ語彙) の索引。
    渡すと候補の provider が実際に実行できるかまで検査し、宣言のみ (例: svn) の候補を可用から外す
    (計画 §19 W1)。None のときは Provider 検査を行わず、catalog 登録だけで判定する従来挙動を保つ。
    """

    bindings = tuple(
        _bind_requirement(
            requirement,
            candidates=candidates,
            registered_capabilities=registered_capabilities,
            installed_provider_capabilities=installed_provider_capabilities,
        )
        for requirement in _object_list(blueprint.get("resource_requirements"))
        if isinstance(requirement.get("key"), str)
    )
    return TaskReadiness(
        level=_readiness_level(
            blueprint,
            bindings=bindings,
            registered_write_capabilities=registered_write_capabilities,
        ),
        requirements=bindings,
    )


def is_deferred_execution_capability(capability: str) -> bool:
    """首版の配備上限を catalog、新規 Run、旧 snapshot で同じ基準にする。"""
    return capability in {"subagent.dispatch/v1", "change.propose/v1"} or (
        is_write_capability(capability) and not capability.startswith("workspace.")
    )


def is_write_capability(capability: str) -> bool:
    """登録済み effect naming から write capability を分類する単一判定。

    資源要求の宣言を blueprint へ一本化した結果、write 資源の要求は apply capability も
    `capabilities` に並べる。Agent の Tool 許可集合や観測 capability の導出では、この判定で
    write を除外しないと承認経路を迂回して apply を呼べてしまう。
    """

    return ".update/" in capability or ".apply/" in capability or ".write/" in capability


def _bind_requirement(
    requirement: Mapping[str, Any],
    *,
    candidates: Sequence[ProjectResourceCandidate],
    registered_capabilities: frozenset[str],
    installed_provider_capabilities: Mapping[str, frozenset[str]] | None = None,
) -> RequirementBinding:
    """一つの要求に照合する候補を選び、不足時は理由を添える。"""

    kind = _string(requirement.get("kind"))
    hints = tuple(_string_list(requirement.get("capabilities")))
    accepted = tuple(_string_list(requirement.get("accepted_providers")))
    required = requirement.get("required") is True
    matched = tuple(
        _rank_candidates(
            [item for item in candidates if _matches(item, kind=kind, hints=hints)],
            accepted_providers=accepted,
        )
    )
    # 「登録済み かつ (Provider 索引を渡されたなら) 実装配線済み」の能力だけを実行可能能力とみなす。
    serviceable = tuple(
        hint
        for hint in hints
        if hint in registered_capabilities
        and (
            installed_provider_capabilities is None
            or bool(installed_provider_capabilities.get(hint))
        )
    )
    runnable = _runnable_candidates(matched, serviceable, installed_provider_capabilities)
    if hints and not serviceable:
        # 平台に未登録、または宣言のみで実装が無い Tool capability しか無い要求は、設定を足しても
        # 安全に自動実行できない。docs/11 §5.2 のとおり発行 gate ではないが GUIDANCE_ONLY へ下がる。
        status = RequirementStatus.UNSUPPORTED
        reason = "No installed Tool capability can serve this resource requirement"
        exposed = matched
    elif runnable:
        status = RequirementStatus.AVAILABLE
        reason = "The project has at least one bindable resource"
        exposed = runnable
    elif matched:
        # 候補は在るが、その Provider の実装が未配線 (例: svn 資源だが svn クライアント未実装)。
        # 束縛しても実行時に落ちるため RUNNABLE と偽らず、正直に設定待ちとして表現する。
        status = RequirementStatus.UNAVAILABLE
        reason = "The bound resource needs a Provider that is not installed yet"
        exposed = ()
    else:
        status = RequirementStatus.UNAVAILABLE
        reason = "The project has no resource bound for this requirement yet"
        exposed = ()
    return RequirementBinding(
        key=cast(str, requirement["key"]),
        kind=kind,
        required=required,
        access=_string(requirement.get("access")),
        status=status,
        reason=reason,
        candidates=exposed,
        capabilities=hints,
        selection_guidance=(
            _string(requirement.get("selection_guidance"))
            or None
        ),
    )


def _runnable_candidates(
    matched: Sequence[ProjectResourceCandidate],
    serviceable: tuple[str, ...],
    installed_provider_capabilities: Mapping[str, frozenset[str]] | None,
) -> tuple[ProjectResourceCandidate, ...]:
    """実行可能能力を、実装配線済みの Provider を持つ候補だけへ絞る。

    Provider 索引が無い (None) 環境では従来どおり照合済み候補をそのまま返す。索引がある場合は、
    候補の provider がその能力の installed Provider 集合に入るものだけを可用とみなす。
    """

    if installed_provider_capabilities is None:
        return tuple(matched)
    return tuple(
        candidate
        for candidate in matched
        if any(
            candidate.provider in installed_provider_capabilities.get(hint, frozenset())
            for hint in serviceable
            if hint in candidate.capabilities
        )
    )


def _matches(
    candidate: ProjectResourceCandidate, *, kind: str, hints: tuple[str, ...]
) -> bool:
    """候補が要求の種別と能力を満たすかを判定する。

    docs/05 §4.2 のとおり ``accepted_providers`` は互換 hint であり束縛の可否を決めない。
    能力と種別を満たす新しい Provider を締め出さないため、ここでは Provider 名で絞らない。
    """

    if candidate.kind != kind:
        return False
    if not hints:
        return True
    return any(hint in candidate.capabilities for hint in hints)


def _rank_candidates(
    candidates: list[ProjectResourceCandidate], *, accepted_providers: tuple[str, ...]
) -> list[ProjectResourceCandidate]:
    """Skill が挙げた Provider を優先しつつ、それ以外も候補として残す。"""

    preferred = set(accepted_providers)
    return sorted(
        candidates,
        key=lambda item: (0 if item.provider in preferred else 1, item.label, item.key),
    )


def _readiness_level(
    blueprint: Mapping[str, Any],
    *,
    bindings: Sequence[RequirementBinding],
    registered_write_capabilities: frozenset[str],
) -> TaskReadinessLevel:
    """逐項束縛状態と effect 意図から Task 全体の就緒度を決める。"""

    required = [item for item in bindings if item.required]
    if any(item.status is RequirementStatus.UNSUPPORTED for item in required):
        return TaskReadinessLevel.GUIDANCE_ONLY
    if any(item.status is RequirementStatus.UNAVAILABLE for item in required):
        return TaskReadinessLevel.CONFIGURATION_REQUIRED
    if _has_executable_apply(blueprint, registered_write_capabilities):
        return TaskReadinessLevel.ACTIONABLE
    # apply 意図があっても write Provider が無ければ ACTIONABLE にしない。docs/04 §3.3 のとおり
    # 提案の生成までは実行でき、実際の書き込みだけが不可能な状態が正しい表現になる。
    return TaskReadinessLevel.RUNNABLE


def _has_executable_apply(
    blueprint: Mapping[str, Any], registered_write_capabilities: frozenset[str]
) -> bool:
    """宣言された apply 意図が登録済み write Provider で実行可能かを判定する。"""

    intents = [
        item for item in _object_list(blueprint.get("effect_intents"))
        if item.get("mode") == "apply"
    ]
    if not intents:
        return False
    resources = {
        key: item
        for item in _object_list(blueprint.get("resource_requirements"))
        if isinstance((key := item.get("key")), str)
    }
    for intent in intents:
        resource = resources.get(_string(intent.get("resource_key")))
        hints = _string_list(resource.get("capabilities")) if resource else []
        if not any(hint in registered_write_capabilities for hint in hints):
            return False
    return True


def _object_list(value: Any) -> list[dict[str, Any]]:
    """Blueprint array から object item だけを返す。"""

    return (
        [cast(dict[str, Any], item) for item in value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value)
        else []
    )


def _string_list(value: Any) -> list[str]:
    """Blueprint array から string item だけを返す。"""

    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _string(value: Any) -> str:
    """空文字を除いた string、または空文字を返す。"""

    return value if isinstance(value, str) and value else ""
