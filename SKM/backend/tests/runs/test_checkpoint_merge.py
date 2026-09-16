"""小さい追加 checkpoint と従来の全量候補が同じ継続状態を保持することを検証する。"""

from __future__ import annotations

from copy import deepcopy

from skillmind.runs.checkpoint import merge_checkpoint


def test_checkpoint_keeps_business_ids_answers_and_refs_without_stale_receipt():
    """モデルが旧状態を再出力しなくても、回答と業務 ID を失わず原回执を混同しない。"""

    previous = {
        "summary": "Earlier step", "confirmed_facts": ["business_id=original"],
        "evidence_refs": ["ev_old"], "artifact_refs": ["art_old"],
        "change_proposal_refs": ["cp_old"], "user_responses": [{"text": "Original answer"}],
        "effect_result": {"outcome": "APPLIED"},
    }
    additions = {
        "summary": "Current step", "confirmed_facts": ["One new fact"],
        "evidence_refs": ["ev_new"], "artifact_refs": [], "change_proposal_refs": [],
    }
    saved_previous, saved_additions = deepcopy(previous), deepcopy(additions)
    merged = merge_checkpoint(previous, additions)
    assert merged["summary"] == "Current step"
    assert merged["confirmed_facts"] == ["business_id=original", "One new fact"]
    assert merged["evidence_refs"] == ["ev_old", "ev_new"]
    assert merged["artifact_refs"] == ["art_old"]
    assert merged["change_proposal_refs"] == ["cp_old"]
    assert merged["user_responses"] == previous["user_responses"]
    assert "effect_result" not in merged
    assert previous == saved_previous and additions == saved_additions
    assert merge_checkpoint(previous, merged) == merged
