"""AgentTaskBrief の組成、ExecutionProfile 合成、prompt 描画の境界を検証する。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skillmind.agent.domain import MaterializedResource, RegisteredTool, RunLimits
from skillmind.agent.task_brief import (
    AGENT_TASK_BRIEF_VERSION,
    ExecutionProfile,
    ProfileSource,
    build_agent_task_brief,
    render_task_brief_prompt,
    resolve_execution_profile,
)
from skillmind.core.hashing import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"

RUN_ID = UUID("00000000-0000-4000-8000-000000000301")
SKILL_VERSION_ID = UUID("00000000-0000-4000-8000-000000000203")
# 蓝图 identity を固定値にしないと fixture 自体が checksum を揺らし、決定性の検証が空振りする。
INTERPRETATION_ID = UUID("00000000-0000-4000-8000-0000000005a1")


def _brief_schema() -> dict[str, Any]:
    """凍結済み AgentTaskBrief contract を読み込む。"""

    return json.loads(
        (CONTRACTS / "agent-task-brief" / "v1.schema.json").read_text(encoding="utf-8")
    )


def _blueprint() -> dict[str, Any]:
    """必須規則・禁止事項・効果意図を宣言した native 蓝图を作る。"""

    return {
        "blueprint_version": "skillmind.capability-blueprint/v1",
        "identity": {
            "skill_key": "generic-native-review",
            "source_hash": "sha256:" + ("a" * 64),
            "interpretation_id": str(INTERPRETATION_ID),
            "interpreter_version": "skillmind-skill-interpreter/2.2.0",
        },
        "compatibility": {"level": "adapted", "confidence": 0.82},
        "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
        "tasks": [
            {
                "key": "review-revision",
                "capability": "repository.review",
                "objective": "Review the selected revision and report evidence-backed findings.",
                "success_criteria": [
                    {"key": "cited", "text": "Every finding cites a path and revision."}
                ],
                "resource_keys": ["target_repository"],
                "deliverables": [
                    {
                        "key": "review-report",
                        "kind": "report",
                        "description": "A Markdown report listing findings and risk.",
                    }
                ],
            }
        ],
        "resource_requirements": [
            {
                "key": "target_repository",
                "kind": "repository",
                "required": True,
                "access": "read",
                "capabilities": ["issue.read/v1"],
                "selection_guidance": "Prefer the revision named by the user.",
            },
            {
                "key": "review_tracker",
                "kind": "issue",
                "required": False,
                "access": "write",
                "capabilities": ["issue.update/v1"],
            },
        ],
        "guidance": {
            "required_rules": [
                {"key": "cite-evidence", "text": "Every finding must cite its source path."}
            ],
            "recommended_steps": [
                {"key": "load-source", "text": "Read the changed files first."}
            ],
            "quality_criteria": [{"key": "actionable", "text": "Say what to change and why."}],
            "prohibited_actions": [
                {"key": "no-silent-fix", "text": "Do not present a rewritten file as reviewed."}
            ],
        },
        "interaction_points": [],
        "effect_intents": [
            {
                "key": "read-repository",
                "mode": "observe",
                "resource_key": "target_repository",
                "operation": "Read the reviewed revision.",
                "risk": "low",
            },
            {
                "key": "prepare-patch",
                "mode": "propose",
                "resource_key": "target_repository",
                "operation": "Prepare a patch and commit plan.",
                "risk": "low",
            },
            {
                "key": "update-tracker",
                "mode": "apply",
                "resource_key": "review_tracker",
                "operation": "Record the review outcome on the tracked issue.",
                "risk": "medium",
                "approval_mode": "ask",
            },
        ],
        "execution_preferences": {
            "recommended_profile": "SUPERVISED",
            "session_split_hints": [],
            "stop_conditions": [
                {"key": "unreadable", "text": "Stop and ask when the revision cannot be read."}
            ],
        },
        "source_traces": [
            {
                "target": "/guidance/required_rules/0",
                "path": "SKILL.md",
                "line": 12,
                "reason": "The Rules section requires evidence citations.",
            }
        ],
        "assumptions": [],
        "questions": [],
    }


def _manifest(*, blueprint: dict[str, Any] | None) -> dict[str, Any]:
    """蓝图の有無を切り替えられる published Manifest を作る。"""

    manifest: dict[str, Any] = {
        "manifest_version": "skillmind/v1alpha1",
        "identity": {"skill_key": "generic-native-review"},
        "compatibility": {"level": "adapted", "confidence": 0.82, "diagnostics": []},
        "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
        "tasks": [
            {
                "key": "review-revision",
                "capability": "repository.review",
                "type": "immediate",
                "workflow": "review-v1",
                "view": "generic-structured",
            }
        ],
        "tools": [{"capability": "issue.read/v1", "required": True}],
        "permissions": {
            "default_tool_policy": "auto",
            "registered_script_policy": "auto",
            "external_write_policy": "deny",
            "write_capabilities": [],
            "network_scope": "project_integrations_only",
        },
    }
    if blueprint is not None:
        manifest["capability_blueprint"] = blueprint
    return manifest


def _task_snapshot(manifest: dict[str, Any]) -> dict[str, Any]:
    """Manifest の実体から checksum を算出した Run task snapshot を作る。"""

    checksum = f"sha256:{sha256_hex(canonical_json(manifest))}"
    return {
        "task_key": "review-revision",
        "capability": "repository.review",
        "skill_version_id": str(SKILL_VERSION_ID),
        "manifest_checksum": checksum,
        "output_schema_checksum": "sha256:" + ("d" * 64),
    }


def _tools() -> tuple[RegisteredTool, ...]:
    """Run に解決済みの読み取り専用 Tool を作る。"""

    return (
        RegisteredTool(
            capability="issue.read/v1",
            sdk_name="mcp__skillmind__issue_read",
            provider="csv",
            integration_id=None,
            input_schema={"type": "object"},
        ),
    )


def _limits() -> RunLimits:
    """M0 platform policy と同じ実行上限を作る。"""

    return RunLimits(max_turns=20, wall_timeout_seconds=900, max_output_bytes=1_048_576)


def _build(
    *,
    blueprint: dict[str, Any] | None = None,
    materialized: Sequence[MaterializedResource] = (),
) -> Any:
    """既定の Run snapshot から Brief を組み立てる。"""

    manifest = _manifest(blueprint=_blueprint() if blueprint is None else blueprint)
    return build_agent_task_brief(
        run_id=RUN_ID,
        task_snapshot=_task_snapshot(manifest),
        manifest=manifest,
        selected_sources={"primary-issues": {"capability": "issue.read/v1", "provider": "csv"}},
        tools=_tools(),
        limits=_limits(),
        materialized=materialized,
    )


def _materialized_repository() -> MaterializedResource:
    """repository 一路を物化した想定の落点記述子。"""

    return MaterializedResource(
        requirement_key="target_repository",
        kind="repository",
        provider="git",
        root="input/target_repository",
        manifest_path="input/target_repository/.skillmind/manifest.json",
        index_path="input/target_repository/.skillmind/files.txt",
        history_path="input/target_repository/.skillmind/history.txt",
        revision="b" * 40,
        files=12,
        skipped=2,
    )


def test_brief_matches_frozen_contract() -> None:
    """組み立てた Brief は公開 contract に適合する。"""

    compiled = _build()
    Draft202012Validator(_brief_schema(), format_checker=FormatChecker()).validate(compiled.brief)
    assert compiled.brief["brief_version"] == AGENT_TASK_BRIEF_VERSION
    assert compiled.checksum.startswith("sha256:")


def test_brief_carries_every_required_rule_without_summarising() -> None:
    """必須規則・禁止事項・品質基準は要約されず全量が Brief と prompt に載る。

    docs/11 §7.1 の「摘要だけを送って required rule を落とさない」を守る境界。ここが緩むと
    Skill が宣言した業務強制が Agent へ届かないまま Run が成功扱いになる。
    """

    compiled = _build()
    guidance = compiled.brief["guidance"]

    assert guidance["required_rules"] == [
        {"key": "cite-evidence", "text": "Every finding must cite its source path."}
    ]
    assert guidance["prohibited_actions"] == [
        {"key": "no-silent-fix", "text": "Do not present a rewritten file as reviewed."}
    ]
    prompt = render_task_brief_prompt(
        compiled.brief, input_json={"target_path": "src/example.py"}, output_schema={}
    )
    assert "Every finding must cite its source path." in prompt
    assert "Do not present a rewritten file as reviewed." in prompt
    assert "Say what to change and why." in prompt
    assert "Stop and ask when the revision cannot be read." in prompt


def test_brief_is_deterministic_for_the_same_snapshot() -> None:
    """同じ凍結 snapshot からは常に同じ checksum が得られる。"""

    assert _build().checksum == _build().checksum


def test_brief_freezes_identity_of_the_interpretation_it_came_from() -> None:
    """Brief は manifest と蓝图の checksum を監査値として固定する。"""

    compiled = _build()
    identity = compiled.brief["identity"]
    blueprint_checksum = f"sha256:{sha256_hex(canonical_json(_blueprint()))}"

    assert identity["run_id"] == str(RUN_ID)
    assert identity["segment_no"] == 1
    assert identity["skill_version_id"] == str(SKILL_VERSION_ID)
    assert identity["blueprint_checksum"] == blueprint_checksum


def test_brief_separates_safe_proposal_from_external_apply() -> None:
    """Propose は成果物まで許可し、apply 宣言を外部 write 権限にしない。

    docs/01 §15.1 の「effect intent 不等于 permission」を Worker 側で保つ。宣言を許可として
    渡すと、Agent は登録済み write Provider も承認も無いまま外部書き込みを試みる。
    """

    compiled = _build()
    intents = {item["key"]: item for item in compiled.brief["effect_policy"]["declared_intents"]}

    assert compiled.brief["effect_policy"]["external_write"] == "deny"
    assert intents["update-tracker"]["mode"] == "apply"
    assert intents["update-tracker"]["executable"] is False
    assert intents["read-repository"]["executable"] is True
    assert intents["prepare-patch"]["executable"] is True
    prompt = render_task_brief_prompt(compiled.brief, input_json={}, output_schema={})
    assert "External writes are denied for this Run." in prompt
    assert "never a permission or an approval" in prompt
    assert "Outcome deliverable" in prompt
    assert "call change.propose/v1" in prompt


def test_brief_binds_only_resources_the_run_selected() -> None:
    """Run が選択した provider だけが束縛済みとして載り、残りは未束縛と明示される。"""

    resources = {item["key"]: item for item in _build().brief["resources"]}

    assert resources["target_repository"]["binding"] == {
        "capability": "issue.read/v1",
        "provider": "csv",
        "binding_source": "RUN_PREFLIGHT",
    }
    assert resources["review_tracker"]["binding"] is None


def test_brief_lists_only_tools_resolved_for_the_run() -> None:
    """許可 Tool は permission snapshot 経由で解決済みのものだけを載せる。"""

    assert _build().brief["allowed_tools"] == [
        {"capability": "issue.read/v1", "provider": "csv", "read_only": True}
    ]


def test_brief_fails_closed_when_the_manifest_declares_no_blueprint() -> None:
    """蓝图を持たない manifest では Brief を組まず、閉じて失敗する。

    発行 gate が蓝图を必須にしている以上、ここへ蓝图無しの manifest が来るのは想定外である。
    黙って空の guidance で組み立てると、Skill の必須規則も目標も渡らないまま Agent が走り、
    しかも Run は成功に見える。
    """

    manifest = _manifest(blueprint=None)
    with pytest.raises(ValueError, match="does not declare a CapabilityBlueprint"):
        build_agent_task_brief(
            run_id=RUN_ID,
            task_snapshot=_task_snapshot(manifest),
            manifest=manifest,
            selected_sources={},
            tools=_tools(),
            limits=_limits(),
        )


def test_brief_keeps_run_objective_when_the_blueprint_task_key_diverges() -> None:
    """蓝图 task key が manifest とずれても capability で目標を取り戻す。

    task key の一致は publish gate で強制していないため、key だけで照合すると目標と成功条件が
    静かに空になる。capability での再照合がその退避路を担保する。
    """

    blueprint = _blueprint()
    blueprint["tasks"][0]["key"] = "review-revision-v2"
    compiled = _build(blueprint=blueprint)

    assert compiled.brief["objective"]["run_objective"] == (
        "Review the selected revision and report evidence-backed findings."
    )
    assert compiled.brief["objective"]["success_criteria"] != []


@pytest.mark.parametrize(
    ("recommended", "expected_profile", "expected_source"),
    [
        (None, ExecutionProfile.SUPERVISED, ProfileSource.PLATFORM_DEFAULT),
        ("GUIDED", ExecutionProfile.GUIDED, ProfileSource.SKILL_RECOMMENDATION),
        ("SUPERVISED", ExecutionProfile.SUPERVISED, ProfileSource.SKILL_RECOMMENDATION),
        ("DELEGATED", ExecutionProfile.SUPERVISED, ProfileSource.PLATFORM_MAXIMUM),
    ],
)
def test_execution_profile_merges_skill_recommendation_under_the_platform_maximum(
    recommended: str | None,
    expected_profile: ExecutionProfile,
    expected_source: ProfileSource,
) -> None:
    """Skill 推奨は platform 上限まで採用し、超過分は降格して由来を残す。

    DELEGATED は ADMIN の事前許可がある資源にしか許されない (docs/11 §7.2)。事前許可が
    未実装のうちに Skill 宣言だけで DELEGATED へ上がれると、来源文書が自律度を決められる。
    """

    preferences: dict[str, Any] = {}
    if recommended is not None:
        preferences["recommended_profile"] = recommended
    resolved = resolve_execution_profile({"execution_preferences": preferences})

    assert resolved.profile is expected_profile
    assert resolved.source is expected_source


def test_prompt_states_that_supervised_may_adapt_the_recommended_steps() -> None:
    """SUPERVISED の prompt は推奨手順の組み替えを許し、必須規則の拘束を明示する。"""

    prompt = render_task_brief_prompt(_build().brief, input_json={}, output_schema={})

    assert "Execution profile SUPERVISED" in prompt
    assert "adapt the recommended steps" in prompt
    assert "required rules and prohibited actions still bind you" in prompt


def test_prompt_keeps_the_deterministic_json_output_contract() -> None:
    """Brief 描画後も M0 の厳密 JSON 出力契約を落とさない。"""

    prompt = render_task_brief_prompt(
        _build().brief,
        input_json={"target_path": "src/example.py"},
        output_schema={"type": "object", "required": ["summary"]},
    )

    assert "Return ONLY one JSON object" in prompt
    assert "Do not use Markdown" in prompt
    assert '"summary"' in prompt
    assert '{"target_path":"src/example.py"}' in prompt


def test_brief_tells_the_agent_where_materialized_resources_are() -> None:
    """物化した資源の落点・索引・清单・履歴・revision を Brief が保持する (計画 §19 W6)。

    §19 は「Agent が既存 workspace.search/read で自走発見する」ことを前提にしているため、
    落点が Brief に無いと発見は模型の勘に依存する。ここが欠けると、資源が在るのに
    「見つからない」と報告される失敗が起き、未束縛との区別が付かない。
    """

    compiled = _build(materialized=[_materialized_repository()])

    Draft202012Validator(_brief_schema(), format_checker=FormatChecker()).validate(compiled.brief)
    placement = next(
        resource["materialization"]
        for resource in compiled.brief["resources"]
        if resource["key"] == "target_repository"
    )
    assert placement == {
        "root": "input/target_repository",
        "manifest": "input/target_repository/.skillmind/manifest.json",
        "index": "input/target_repository/.skillmind/files.txt",
        "history": "input/target_repository/.skillmind/history.txt",
        "revision": "b" * 40,
        "materialized_files": 12,
        "skipped_files": 2,
    }
    # 物化していない要求には落点を付けない (存在しない path を案内しない)。
    tracker = next(
        resource for resource in compiled.brief["resources"] if resource["key"] == "review_tracker"
    )
    assert "materialization" not in tracker


def test_brief_omits_materialization_when_nothing_was_materialized() -> None:
    """未物化 (offline/未配線) の Run では落点を一切書かない。"""

    compiled = _build()

    assert all("materialization" not in item for item in compiled.brief["resources"])


def test_prompt_points_the_agent_at_the_materialized_tree_and_skipped_meaning() -> None:
    """prompt が落点・索引・履歴と `skipped` の意味を明示する (計画 §19 W6)。"""

    compiled = _build(materialized=[_materialized_repository()])

    prompt = render_task_brief_prompt(
        compiled.brief, input_json={"ticket_id": "T-1"}, output_schema={"type": "object"}
    )

    assert "Materialized resources" in prompt
    assert "input/target_repository/" in prompt
    assert "input/target_repository/.skillmind/files.txt" in prompt
    assert "input/target_repository/.skillmind/history.txt" in prompt
    assert "b" * 40 in prompt
    # 「読めなかった」を「存在しない」と報告させないための一文を必ず載せる。
    assert "never" in prompt and "missing" in prompt


def test_prompt_has_no_materialization_section_without_materialized_resources() -> None:
    """未物化なら prompt にも案内を出さない (存在しない directory を示唆しない)。"""

    compiled = _build()

    prompt = render_task_brief_prompt(
        compiled.brief, input_json={"ticket_id": "T-1"}, output_schema={"type": "object"}
    )

    assert "Materialized resources" not in prompt
