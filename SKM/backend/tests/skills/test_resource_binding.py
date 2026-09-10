"""資源要求の照合と Project ごとの就緒度判定を検証する。"""

from __future__ import annotations

from typing import Any

from skillmind.api.routes.skills import _readiness_response
from skillmind.skills.resource_binding import (
    ProjectResourceCandidate,
    RequirementStatus,
    TaskReadinessLevel,
    evaluate_blueprint_readiness,
)

REGISTERED = frozenset({"document.read/v1", "issue.read/v1", "repository.read/v1"})


def _document_candidate(name: str = "checklist.md") -> ProjectResourceCandidate:
    """Project 文書由来の束縛候補を返す。"""

    return ProjectResourceCandidate(
        key=f"review/{name}",
        kind="document",
        provider="project-documents",
        label=f"review/{name}",
        capabilities=("document.read/v1",),
    )


def _blueprint(*requirements: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """指定した資源要求を持つ最小蓝图を返す。"""

    return {
        "blueprint_version": "skillmind.capability-blueprint/v1",
        "capabilities": [{"key": "doc.review", "title": "Document Review"}],
        "tasks": [
            {"key": "review", "capability": "doc.review", "objective": "Review the document."}
        ],
        "resource_requirements": list(requirements),
        **extra,
    }


def _document_requirement(*, required: bool = True, **extra: Any) -> dict[str, Any]:
    """document.read/v1 を要求する資源要求を返す。"""

    return {
        "key": "review_notes",
        "kind": "document",
        "required": required,
        "access": "read",
        "capabilities": ["document.read/v1"],
        **extra,
    }


def test_same_blueprint_is_runnable_only_where_the_resource_exists() -> None:
    """同じ SkillVersion が Project の資源保有状況で異なる就緒度になる。

    これが S2 の受入判据であり、資源不足は曖昧な gate ではなく設定待ちとして表現する。
    """

    blueprint = _blueprint(_document_requirement())

    configured = evaluate_blueprint_readiness(
        blueprint, candidates=[_document_candidate()], registered_capabilities=REGISTERED
    )
    empty = evaluate_blueprint_readiness(
        blueprint, candidates=[], registered_capabilities=REGISTERED
    )

    assert configured.level is TaskReadinessLevel.RUNNABLE
    assert empty.level is TaskReadinessLevel.CONFIGURATION_REQUIRED


def test_missing_resource_reports_the_requirement_not_a_blanket_gate() -> None:
    """不足時も逐項の要求・理由・候補を返し、単一の gate 失敗にまとめない。"""

    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement(selection_guidance="Pick the review checklist.")),
        candidates=[],
        registered_capabilities=REGISTERED,
    )
    requirement = readiness.requirements[0]

    assert requirement.key == "review_notes"
    assert requirement.status is RequirementStatus.UNAVAILABLE
    assert requirement.candidates == ()
    assert requirement.reason
    assert requirement.selection_guidance == "Pick the review checklist."


def test_optional_resource_does_not_block_runnability() -> None:
    """任意資源が未束縛でも実行可能性は下がらない。"""

    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement(required=False)),
        candidates=[],
        registered_capabilities=REGISTERED,
    )

    assert readiness.level is TaskReadinessLevel.RUNNABLE
    assert readiness.requirements[0].status is RequirementStatus.UNAVAILABLE


def test_unregistered_capability_downgrades_to_guidance_only() -> None:
    """登録済み Tool capability が無い要求は設定しても自動実行できない。"""

    readiness = evaluate_blueprint_readiness(
        _blueprint(
            {
                "key": "wiki",
                "kind": "other",
                "required": True,
                "access": "read",
                "capabilities": ["wiki.read/v1"],
            }
        ),
        candidates=[],
        registered_capabilities=REGISTERED,
    )

    assert readiness.level is TaskReadinessLevel.GUIDANCE_ONLY
    assert readiness.requirements[0].status is RequirementStatus.UNSUPPORTED


# 実装配線済み Provider の索引 (能力→候補語彙の provider 名)。本番 api の注入と同形。svn は除く。
INSTALLED_PROVIDERS = {
    "issue.read/v1": frozenset({"redmine"}),
    "repository.read/v1": frozenset({"git"}),
    "document.read/v1": frozenset({"project-documents"}),
}


def _repository_requirement(*, required: bool = True, **extra: Any) -> dict[str, Any]:
    """repository.read/v1 を要求する資源要求を返す。"""

    return {
        "key": "source_repository",
        "kind": "repository",
        "required": required,
        "access": "read",
        "capabilities": ["repository.read/v1"],
        **extra,
    }


def _repository_candidate(provider: str) -> ProjectResourceCandidate:
    """指定 provider の repository 束縛候補を返す。"""

    return ProjectResourceCandidate(
        key=f"repo/{provider}",
        kind="repository",
        provider=provider,
        label=f"repo/{provider}",
        capabilities=("repository.read/v1",),
    )


def test_declared_but_uninstalled_provider_is_not_runnable() -> None:
    """svn は宣言済みだが実装未配線。svn だけの候補は RUNNABLE と偽らず設定待ちにする (§19 W1)。

    これを AVAILABLE のままにすると就緒度が RUNNABLE を示し、preflight/実行で
    `Tool Provider is not installed` に至る。宣言と実行可能性の乖離を就緒度で正直に表す。
    """

    readiness = evaluate_blueprint_readiness(
        _blueprint(_repository_requirement()),
        candidates=[_repository_candidate("svn")],
        registered_capabilities=REGISTERED,
        installed_provider_capabilities=INSTALLED_PROVIDERS,
    )

    binding = readiness.requirements[0]
    assert binding.status is RequirementStatus.UNAVAILABLE
    assert "not installed" in binding.reason
    # 束縛すると実行時に落ちる候補は提示しない。
    assert binding.candidates == ()
    assert readiness.level is TaskReadinessLevel.CONFIGURATION_REQUIRED


def test_installed_provider_candidate_is_available() -> None:
    """git は実装配線済み。git 候補は AVAILABLE となり RUNNABLE に達する。"""

    candidate = _repository_candidate("git")
    readiness = evaluate_blueprint_readiness(
        _blueprint(_repository_requirement()),
        candidates=[candidate],
        registered_capabilities=REGISTERED,
        installed_provider_capabilities=INSTALLED_PROVIDERS,
    )

    binding = readiness.requirements[0]
    assert binding.status is RequirementStatus.AVAILABLE
    assert binding.candidates == (candidate,)
    assert readiness.level is TaskReadinessLevel.RUNNABLE


def test_uninstalled_candidate_is_filtered_out_when_an_installed_one_exists() -> None:
    """git と svn が併存するなら、実行できる git だけを可用候補として残す。"""

    git = _repository_candidate("git")
    svn = _repository_candidate("svn")
    readiness = evaluate_blueprint_readiness(
        _blueprint(_repository_requirement()),
        candidates=[svn, git],
        registered_capabilities=REGISTERED,
        installed_provider_capabilities=INSTALLED_PROVIDERS,
    )

    binding = readiness.requirements[0]
    assert binding.status is RequirementStatus.AVAILABLE
    assert [item.provider for item in binding.candidates] == ["git"]


def test_provider_check_is_opt_in_and_preserves_legacy_behavior() -> None:
    """Provider 索引を渡さない環境では従来どおり catalog 登録だけで判定する。

    offline/未配線環境が既存挙動を保つための後方互換。svn 候補も従来どおり AVAILABLE になる。
    """

    svn = _repository_candidate("svn")
    readiness = evaluate_blueprint_readiness(
        _blueprint(_repository_requirement()),
        candidates=[svn],
        registered_capabilities=REGISTERED,
    )

    binding = readiness.requirements[0]
    assert binding.status is RequirementStatus.AVAILABLE
    assert binding.candidates == (svn,)
    assert readiness.level is TaskReadinessLevel.RUNNABLE


def test_document_candidate_stays_available_under_provider_check() -> None:
    """Provider 語彙が候補と一致するため、document 候補は Provider 検査下でも可用のまま。"""

    candidate = _document_candidate()
    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement()),
        candidates=[candidate],
        registered_capabilities=REGISTERED,
        installed_provider_capabilities=INSTALLED_PROVIDERS,
    )

    binding = readiness.requirements[0]
    assert binding.status is RequirementStatus.AVAILABLE
    assert binding.candidates == (candidate,)


def test_unlisted_provider_can_still_bind_a_matching_capability() -> None:
    """accepted_providers は互換 hint であり、能力を満たす Provider を締め出さない。

    docs/05 §4.2 のとおり、列挙されていない新しい Provider も能力と種別を満たせば候補になる。
    """

    candidate = ProjectResourceCandidate(
        key="wiki/page",
        kind="document",
        provider="confluence",
        label="wiki/page",
        capabilities=("document.read/v1",),
    )
    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement(accepted_providers=["project-documents"])),
        candidates=[candidate],
        registered_capabilities=REGISTERED,
    )

    assert readiness.level is TaskReadinessLevel.RUNNABLE
    assert readiness.requirements[0].candidates == (candidate,)


def test_listed_providers_are_ranked_before_other_candidates() -> None:
    """Skill が挙げた Provider を先頭に並べつつ、他の候補も残す。"""

    listed = _document_candidate("a-listed.md")
    other = ProjectResourceCandidate(
        key="review/b-other.md",
        kind="document",
        provider="confluence",
        label="review/b-other.md",
        capabilities=("document.read/v1",),
    )
    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement(accepted_providers=["project-documents"])),
        candidates=[other, listed],
        registered_capabilities=REGISTERED,
    )

    assert [item.provider for item in readiness.requirements[0].candidates] == [
        "project-documents",
        "confluence",
    ]


def test_candidate_of_another_kind_is_not_matched() -> None:
    """種別の異なる資源を要求へ誤って束縛しない。"""

    issue = ProjectResourceCandidate(
        key="redmine/1",
        kind="issue",
        provider="redmine",
        label="#1",
        capabilities=("issue.read/v1",),
    )
    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement()),
        candidates=[issue],
        registered_capabilities=REGISTERED,
    )

    assert readiness.level is TaskReadinessLevel.CONFIGURATION_REQUIRED
    assert readiness.requirements[0].candidates == ()


def test_apply_intent_without_a_write_provider_stays_runnable() -> None:
    """write Provider が無い apply 意図は提案止まりであり ACTIONABLE にしない。

    docs/04 §3.3 のとおり、この状態でも分析と提案生成までは実行できる。
    """

    blueprint = _blueprint(
        _document_requirement(),
        {
            "key": "tracker",
            "kind": "issue",
            "required": False,
            "access": "write",
            "capabilities": ["issue.update/v1"],
        },
        effect_intents=[
            {
                "key": "update-tracker",
                "mode": "apply",
                "resource_key": "tracker",
                "operation": "Record the outcome.",
                "risk": "medium",
                "approval_mode": "ask",
            }
        ],
    )

    readiness = evaluate_blueprint_readiness(
        blueprint,
        candidates=[_document_candidate()],
        registered_capabilities=REGISTERED,
        registered_write_capabilities=frozenset(),
    )

    assert readiness.level is TaskReadinessLevel.RUNNABLE


def test_apply_intent_with_a_registered_write_provider_is_actionable() -> None:
    """登録済み write Provider が揃った apply 意図だけが ACTIONABLE になる。"""

    blueprint = _blueprint(
        _document_requirement(),
        {
            "key": "tracker",
            "kind": "issue",
            "required": False,
            "access": "write",
            "capabilities": ["issue.update/v1"],
        },
        effect_intents=[
            {
                "key": "update-tracker",
                "mode": "apply",
                "resource_key": "tracker",
                "operation": "Record the outcome.",
                "risk": "medium",
                "approval_mode": "ask",
            }
        ],
    )

    readiness = evaluate_blueprint_readiness(
        blueprint,
        candidates=[_document_candidate()],
        registered_capabilities=REGISTERED | {"issue.update/v1"},
        registered_write_capabilities=frozenset({"issue.update/v1"}),
    )

    assert readiness.level is TaskReadinessLevel.ACTIONABLE


def test_blueprint_without_resources_is_runnable_as_advice() -> None:
    """資源を要求しない助言型 Skill は設定待ちにしない。"""

    readiness = evaluate_blueprint_readiness(
        _blueprint(), candidates=[], registered_capabilities=REGISTERED
    )

    assert readiness.level is TaskReadinessLevel.RUNNABLE
    assert readiness.requirements == ()


def test_api_readiness_response_carries_per_requirement_evidence() -> None:
    """公開 response が就緒度 level だけでなく逐項の根拠まで運ぶ。

    level だけを返すと Workspace は「何を設定すればよいか」を示せず、S2 が避けようとした
    曖昧な gate 表示へ戻ってしまう。
    """

    readiness = evaluate_blueprint_readiness(
        _blueprint(_document_requirement(selection_guidance="Pick the checklist.")),
        candidates=[_document_candidate()],
        registered_capabilities=REGISTERED,
    )

    response = _readiness_response(readiness)

    assert response is not None
    assert response.level == "RUNNABLE"
    requirement = response.requirements[0]
    assert requirement.key == "review_notes"
    assert requirement.status == "AVAILABLE"
    assert requirement.selection_guidance == "Pick the checklist."
    assert [item.label for item in requirement.candidates] == ["review/checklist.md"]


def test_api_readiness_response_is_null_when_undetermined() -> None:
    """Catalog 未配線の環境では未判定を null で表し、実行不可と断定しない。"""

    assert _readiness_response(None) is None
