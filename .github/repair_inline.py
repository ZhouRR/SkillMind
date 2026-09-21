"""検証で見つかった局所修正を、原 patch の同じ byte にだけ適用する。"""
from pathlib import Path


def once(text, old, new):
    """一致箇所が一つだけの場合に差し替え、上流変更を黙って上書きしない。"""
    assert text.count(old) == 1, (old[:100], text.count(old))
    return text.replace(old, new, 1)


p = Path('SKM/backend/src/skillmind/agent/engine.py')
t = p.read_text()
t = once(t, 'from collections.abc import ', 'from collections.abc import Awaitable, ')
p.write_text(t)
p = Path('SKM/backend/src/skillmind/runs/repository_effects.py')
t = p.read_text()
start = t.index('    async def finalize_effect_execution(')
end = t.index('    async def _finish_effect_continuation(', start)
part = t[start:end]
for line in ('            outcome = "APPLIED"\n', '            event_type = AgentEventType.EFFECT_APPLIED\n', '            outcome = failure.status.value\n', '            event_type = AgentEventType.EFFECT_FAILED\n'):
    part = once(part, line, '')
t = t[:start] + part + t[end:]
# Segment の既存 checkpoint_json のみ更新する。架空の列や凍結済み Brief identity を変更しない。
t = once(t, '        segment.checkpoint_checksum = "sha256:" + sha256_hex(canonical_json(checkpoint))\n', '')
# APPLIED の新経路は必ず本来の二つの Evidence を持つ。None を文字列化・省略しない。
t = once(t,
    '        if execution is None or execution.status != EffectExecutionStatus.APPLIED.value:\n            return None\n',
    '        if execution is None or execution.status != EffectExecutionStatus.APPLIED.value:\n            return None\n'
    '        if not isinstance(execution.before_ref, str) or not isinstance(execution.after_ref, str):\n'
    '            raise EffectLeaseValidationError("Inline receipt references are unavailable")\n')
t = once(t, '            if execution.status == "APPLIED":\n                after = ',
    '            if execution.status == "APPLIED":\n'
    '                if not isinstance(execution.before_ref, str) or not isinstance(execution.after_ref, str):\n'
    '                    raise EffectLeaseValidationError("Inline receipt references are unavailable")\n'
    '                after = ')
t = once(t, '                evidence_refs=(execution.before_ref, execution.after_ref) if result else (), now=now)',
    '                evidence_refs=(result["before_ref"], result["after_ref"]) if result else (), now=now)')
t = once(t, '            agent_session = await self._session.scalar(select(AgentSession).where(',
    '            active_session = await self._session.scalar(select(AgentSession).where(')
t = once(t, '            if agent_session is None:\n                raise LeaseValidationError("Inline SDK session is not active")\n',
    '            if active_session is None:\n                raise LeaseValidationError("Inline SDK session is not active")\n'
    '            agent_session = active_session\n')
p.write_text(t)

p = Path('SKM/backend/tests/runs/test_inline_effect_lane.py')
t = p.read_text()
assert 'def receipt_case' not in t
t += '''

def receipt_case():
    """本来の回执と可変 checkpoint を用意し、DB I/O だけを置換する。"""
    from unittest.mock import MagicMock
    from skillmind.core.hashing import canonical_json, sha256_hex
    from skillmind.db.models import EffectExecution, Evidence

    h = inline_case()
    parent = h.rows[RunAttempt]
    parent.id = h.proposal.run_attempt_id
    h.segment.checkpoint_json = {"summary": "Keep previous facts.", "evidence_refs": ["ev_previous"]}
    h.proposal.checkpoint_json = {"summary": "Frozen proposal."}
    h.repository._lock_claimed_execution = AsyncMock(return_value=(h.run, h.segment, parent))
    h.repository._validate_claimed_lease = MagicMock()
    h.repository._reject_cancelled_execution = AsyncMock()
    h.execution.status = "APPLIED"
    h.execution.before_ref, h.execution.after_ref = "ev_before_original", "ev_after_original"
    content = {"actual": "hallo\\r"}
    h.after = SimpleNamespace(
        tool_call_id=h.execution.tool_call_id,
        metadata_json={"snapshot": content},
        content_hash="sha256:" + sha256_hex(canonical_json(content)),
    )

    def scalar(statement):
        """Effect と Evidence の集合検索へ原行を返す。"""
        entity = statement.column_descriptions[0]["entity"]
        return {EffectExecution: h.execution, Evidence: h.after}[entity]

    h.session.scalar = AsyncMock(side_effect=scalar)
    return h


async def test_inline_receipt_preserves_raw_value_and_real_checkpoint_fields():
    """存在しない checksum 列や不変提案を変更せず、原値を一度の交付へ渡す。"""
    h = receipt_case()
    result = await h.repository.inline_receipt(h.claimed, h.proposal.id)
    assert result["after"] == {"actual": "hallo\\r"}
    assert result["before_ref"] == "ev_before_original"
    assert result["after_ref"] == "ev_after_original"
    assert h.segment.checkpoint_json["evidence_refs"] == ["ev_previous", "ev_before_original", "ev_after_original"]
    assert h.segment.checkpoint_json["change_proposal_refs"] == [h.proposal.proposal_ref]
    assert not hasattr(h.segment, "checkpoint_checksum")
    assert h.proposal.checkpoint_json == {"summary": "Frozen proposal."}
    assert h.proposal.inline_owner_json["phase"] == "DELIVERED"
    assert h.rows[RunAttempt].inline_proposal_id is None
    assert h.run.status == h.segment.status == "RUNNING"
    h.repository._next_effect_segment.assert_not_called()


@pytest.mark.parametrize("field", ["before_ref", "after_ref"])
async def test_missing_inline_reference_is_not_delivered_or_defaulted(field):
    """APPLIED 表示だけで欠落した証拠を補完して model を進めない。"""
    h = receipt_case()
    setattr(h.execution, field, None)
    with pytest.raises(EffectLeaseValidationError):
        await h.repository.inline_receipt(h.claimed, h.proposal.id)
    assert h.proposal.inline_owner_json["phase"] == "ACTIVE"
    assert h.rows[RunAttempt].inline_proposal_id == h.proposal.id


async def test_detaching_confirmed_effect_uses_exact_saved_references():
    """応答断線後も元操作だけを通常続行へ渡し、新規 apply を配送しない。"""
    h = receipt_case()
    h.repository._finish_effect_continuation = AsyncMock()
    await h.repository.detach_inline_effect(
        h.run, h.segment, h.rows[RunAttempt], h.proposal, now=datetime.now(UTC)
    )
    args = h.repository._finish_effect_continuation.await_args.kwargs
    assert args["evidence_refs"] == ("ev_before_original", "ev_after_original")
    assert args["effect_result"]["after"] == {"actual": "hallo\\r"}
    assert args["execution"] is h.execution and args["failure"] is None
    assert h.proposal.inline_owner_json["phase"] == "DETACHED"
    assert h.rows[RunAttempt].inline_proposal_id is None
    h.session.add.assert_not_called()
'''
p.write_text(t)
