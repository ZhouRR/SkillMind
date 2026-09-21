"""原生成物の限定修正と回帰を同じ準備 pipeline へ適用する。"""
from pathlib import Path

p = Path('SKM/backend/src/skillmind/runs/repository_effects.py')
t = p.read_text()
old = '        checkpoint = deepcopy(segment.checkpoint_json or {})\n'
assert t.count(old) == 1
t = t.replace(old, '        checkpoint = merge_checkpoint(segment.checkpoint_json or {}, proposal.checkpoint_json)\n', 1)
p.write_text(t)

p = Path('SKM/backend/tests/runs/test_inline_effect_lane.py')
t = p.read_text()
assert 'test_inline_delivery_retains_new_business_facts' not in t
t += '''

async def test_inline_delivery_retains_new_business_facts_and_is_idempotent():
    """原提案で確認した新事実/参照を持続させ、再交付で重複しない。"""
    from copy import deepcopy

    h = receipt_case()
    h.segment.checkpoint_json["confirmed_facts"] = ["Earlier registration completed."]
    h.proposal.checkpoint_json.update(
        confirmed_facts=["Result artifact has been created."],
        artifact_refs=["art_saved_result"],
    )
    original = deepcopy(h.proposal.checkpoint_json)
    first = await h.repository.inline_receipt(h.claimed, h.proposal.id)
    second = await h.repository.inline_receipt(h.claimed, h.proposal.id)
    assert first == second
    assert h.segment.checkpoint_json["confirmed_facts"] == [
        "Earlier registration completed.", "Result artifact has been created."
    ]
    assert h.segment.checkpoint_json["artifact_refs"] == ["art_saved_result"]
    assert h.segment.checkpoint_json["evidence_refs"] == [
        "ev_previous", "ev_before_original", "ev_after_original"
    ]
    assert h.proposal.checkpoint_json == original
    h.repository._next_effect_segment.assert_not_called()


async def test_active_inline_cannot_use_another_pending_proposal():
    """同じ Run/Attempt でも待機中の原提案と異なる所有権を借用しない。"""
    from uuid import uuid4

    h = inline_case()
    h.rows[RunAttempt].inline_proposal_id = uuid4()
    with pytest.raises(EffectLeaseValidationError):
        await h.repository._inline_parent_active(h.run, h.segment, h.proposal)
'''
p.write_text(t)
