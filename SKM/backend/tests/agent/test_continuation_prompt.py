"""続行の差分化が原文・入力・権限・原回执を失わず、旧会話へも互換であることを検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from skillmind.agent.continuation_prompt import continuation_prompt
from skillmind.agent.domain import RunContext
from skillmind.agent.task_brief import render_task_brief_prompt
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.runs.checkpoint import merge_checkpoint
from tests.agent.test_task_brief import _build
from tests.agent.test_tool_gateway import CsvIssueProvider, _context, _registry
from tests.runs.test_effect_continuation import receipt


def context_with_brief(tmp_path: Path, *, segment: int = 1, checkpoint=None) -> RunContext:
    """実 Brief builder の出力に、変更を検出できる原文と業務入力を加える。"""

    context = _context(tmp_path, _registry(CsvIssueProvider()))
    brief = _build(segment_no=segment, checkpoint=checkpoint).brief
    source = "Use exact_table.exact_column and results/{business_id}/report.md.\n" * 100
    brief["source_documents"] = [{
        "path": "SKILL.md", "content": source, "sha256": f"sha256:{sha256_hex(source)}",
    }]
    return compiled(replace(context, input_json={"selection": "original"}), brief)


def compiled(context: RunContext, brief: dict) -> RunContext:
    """現在の完全な Brief と提示文を同じ hash で凍結する。"""

    return replace(
        context, task_brief=brief,
        task_brief_checksum=f"sha256:{sha256_hex(canonical_json(brief))}",
        prompt=render_task_brief_prompt(
            brief, input_json=context.input_json, output_schema=context.result_schema,
        ),
    )


def test_same_session_sends_new_receipt_without_repeating_source_and_old_facts(tmp_path):
    """全量監査を保ったまま、続行 prompt には新回执の原値だけを追加する。"""

    old = context_with_brief(tmp_path, checkpoint={
        "summary": "Ready", "confirmed_facts": ["business_id=original"],
        "evidence_refs": ["ev_original"],
    })
    first, metadata = continuation_prompt(old, old.prompt, None)
    new_brief = deepcopy(old.task_brief)
    new_brief["identity"]["segment_no"] = 2
    new_brief["objective"]["segment_objective"] = "Continue after the saved report."
    new_brief["checkpoint"]["summary"] = "Save succeeded"
    new_brief["checkpoint"]["effect_result"] = receipt()
    new_brief["checkpoint"]["evidence_refs"].append("ev_new")
    current = compiled(old, new_brief)
    delta, _ = continuation_prompt(current, current.prompt, metadata)
    assert first == old.prompt and "exact_table.exact_column" in first
    assert "exact_table.exact_column" not in delta and "business_id=original" not in delta
    assert '"ev_new"' in delta and '"ev_original"' not in delta
    assert canonical_json(receipt()) in delta
    assert "Continue after the saved report." in delta
    assert len(delta) < len(current.prompt) / 2
    assert current.task_brief == new_brief


@pytest.mark.parametrize("change", ["legacy", "input", "source", "permission", "tool", "rule"])
def test_legacy_or_changed_static_context_receives_full_instructions(tmp_path, change):
    """新版指示・原文・入力・権限・Tool が変われば過去 hash から省略を許可しない。"""

    original = context_with_brief(tmp_path)
    _, metadata = continuation_prompt(original, original.prompt, None)
    current = original
    brief = deepcopy(original.task_brief)
    if change == "legacy":
        metadata = {}
    elif change == "input":
        current = replace(current, input_json={"selection": "changed"})
    elif change == "source":
        document = brief["source_documents"][0]
        document["content"] = "Use the changed exact_table.other_column."
        document["sha256"] = f"sha256:{sha256_hex(document['content'])}"
    elif change == "permission":
        current = replace(current, permission_snapshot={"allowed_capabilities": []})
    elif change == "tool":
        current = replace(current, tools=())
    else:
        brief["guidance"]["required_rules"].append({"key": "new", "text": "New exact rule."})
    current = compiled(current, brief)
    assert continuation_prompt(current, current.prompt, metadata)[0] == current.prompt


def test_corrupt_brief_cannot_be_used_for_delta_resume(tmp_path):
    """壊れた元指示を既送扱いせず model 開始前に拒否する。"""

    context = context_with_brief(tmp_path)
    context.task_brief["checkpoint"]["summary"] = "Changed without checksum"
    with pytest.raises(ValueError, match="checksum"):
        continuation_prompt(context, context.prompt, {})


def test_legacy_rewritten_fact_lists_are_explicit_replacements_without_source_repetition(tmp_path):
    """旧実行の全量再要約も、新しい list と古い成功回执の解除を正確に伝える。"""

    context = context_with_brief(tmp_path, checkpoint={
        "summary": "Before", "confirmed_facts": ["Old wording"], "effect_result": receipt(),
    })
    _, metadata = continuation_prompt(context, context.prompt, None)
    brief = deepcopy(context.task_brief)
    brief["identity"]["segment_no"] = 2
    brief["checkpoint"]["confirmed_facts"] = ["Rewritten wording"]
    del brief["checkpoint"]["effect_result"]
    current = compiled(context, brief)
    prompt, _ = continuation_prompt(current, current.prompt, metadata)
    assert "Frozen Skill source documents" not in prompt
    assert 'Replacement checkpoint lists (JSON): ["confirmed_facts"]' in prompt
    assert '"effect_result":null' in prompt
    assert "Rewritten wording" in prompt and "Old wording" not in prompt


def test_fifteen_continuations_do_not_repeat_the_static_skill(tmp_path):
    """多数の write でも、毎回の全文再投入による入力増加を再発させない。"""

    context = context_with_brief(tmp_path)
    previous = None
    sent = []
    full = []
    for segment in range(1, 16):
        brief = deepcopy(context.task_brief)
        brief["identity"]["segment_no"] = segment
        brief["checkpoint"] = merge_checkpoint(brief["checkpoint"], {
            "summary": f"Step {segment}", "confirmed_facts": [f"New fact {segment}"],
        })
        context = compiled(context, brief)
        prompt, previous = continuation_prompt(context, context.prompt, previous)
        sent.append(prompt)
        full.append(context.prompt)
    assert sum("Frozen Skill source documents" in prompt for prompt in sent) == 1
    assert sum(map(len, sent)) < sum(map(len, full)) * 0.25


def test_source_execution_resumes_without_repeating_original_skill(tmp_path):
    """原文方式の Brief に旧 objective を要求せず、同一会話の差分続行を保つ。"""
    import json

    context = _context(tmp_path, _registry(CsvIssueProvider()))
    source = Path(__file__).resolve().parents[3] / "contracts/examples/agent-task-brief.v2.json"
    original = compiled(context, json.loads(source.read_text()))
    first, metadata = continuation_prompt(original, original.prompt, None)
    changed = deepcopy(original.task_brief)
    changed["identity"]["segment_no"] = 2
    changed["checkpoint"] = {"summary": "Continue", "confirmed_facts": ["record=original"]}
    resumed = compiled(context, changed)
    delta, _ = continuation_prompt(resumed, resumed.prompt, metadata)
    assert first == original.prompt
    assert "record=original" in delta
    assert "Frozen Skill sources" not in delta
    assert "Continue the same Run" in delta
    changed["task"]["description"] += " changed"
    replaced = compiled(context, changed)
    full, _ = continuation_prompt(replaced, replaced.prompt, metadata)
    assert full == replaced.prompt


def test_schema_observations_are_exact_deltas_not_rewritten_checkpoints(tmp_path):
    """構造の証拠・観測時刻は原値で渡し、同一観測を次回再送しない。"""
    old = context_with_brief(tmp_path)
    _, metadata = continuation_prompt(old, old.prompt, None)
    brief = deepcopy(old.task_brief)
    observation = {"table": "public.reports", "observed_at": "2026-09-16T01:00:00Z",
                   "observation_refs": ["ev_original"], "table_schema": {"primary_key": ["b", "a"]}}
    brief["checkpoint"]["database_observations"] = [observation]
    current = compiled(old, brief)
    delta, updated = continuation_prompt(current, current.prompt, metadata)
    assert canonical_json(observation) in delta
    repeated, _ = continuation_prompt(current, current.prompt, updated)
    assert '"ev_original"' not in repeated
    assert "exact_table.exact_column" not in repeated
