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

p = Path('SKM/backend/src/skillmind/agent/task_brief.py')
t = p.read_text()
old = '''        "effect_result; it is not a current-state observation or business PASS. Continue normally "
        "after a confirmed response, and stop only when the tool returns paused."
'''
new = '''        "effect_result; it is not a current-state observation or business PASS. A confirmed "
        "delivery does not satisfy the Skill's business continuation conditions by itself. "
        "Apply required checks and stop on known failures or unmet conditions even when "
        "the Effect is APPLIED. Do not pause merely to request the same acknowledged receipt; "
        "always stop this native turn when the tool returns paused."
'''
assert t.count(old) == 1
t = t.replace(old, new, 1)
p.write_text(t)

p = Path('SKM/backend/tests/effects/test_compact_proposal.py')
t = p.read_text()
assert 'test_inline_prompt_preserves_business_stop_conditions' not in t
t += '''

def test_inline_prompt_preserves_business_stop_conditions():
    """回执の確定を、Skill の条件を無視した次操作の許可と混同しない。"""
    from skillmind.agent.task_brief import render_task_brief_prompt
    from tests.agent.test_task_brief import _build

    prompt = render_task_brief_prompt(_build().brief, input_json={}, output_schema={})
    assert "does not satisfy the Skill's business continuation conditions" in prompt
    assert "stop on known failures or unmet conditions" in prompt
    assert "always stop this native turn when the tool returns paused" in prompt
    assert "stop only when" not in prompt


@pytest.mark.parametrize("status", ["ERROR", "TIMEOUT"])
def test_inline_success_preserves_confirmed_business_failure(status):
    """配送成功は実操作の成功でない。元の失敗状態と回读の意味を保持する。"""
    from skillmind.effects.inline import inline_success
    from tests.runs.test_effect_continuation import receipt

    original = receipt()
    original["capability_version"] = "mcp.call/v1"
    original["after"] = {"read_back": {"status": status, "actual": "hallo\\r"}}
    original["after_content_hash"] = "sha256:" + sha256_hex(canonical_json(original["after"]))
    original["verification"] = {"business_verdict": "NOT_EVALUATED"}
    before = deepcopy(original)
    result = inline_success(original)
    assert original == before
    assert result["effect_result"] == before
    assert result["effect_result"]["after"]["read_back"]["status"] == status
    assert result["effect_result"]["verification"]["business_verdict"] == "NOT_EVALUATED"
'''
p.write_text(t)


def replace_once(path, old, new):
    """同じ原コードにだけ時限予約を加える。"""
    p = Path(path)
    t = p.read_text()
    assert t.count(old) == 1, (path, old[:80], t.count(old))
    p.write_text(t.replace(old, new, 1))


replace_once('SKM/backend/src/skillmind/worker/tool_authority.py',
    '    _active: bool = True\n',
    '    _active: bool = True\n'
    '    # 最適化の経路選択だけに使う。既存 supervisor の期限は変更しない。\n'
    '    deadline: float | None = field(default=None, repr=False)\n')
replace_once('SKM/backend/src/skillmind/worker/tool_authority.py',
    'def bind_tool_authority(claimed: ClaimedRun) -> Iterator[None]:',
    'def bind_tool_authority(claimed: ClaimedRun, *, deadline: float | None = None) -> Iterator[None]:')
replace_once('SKM/backend/src/skillmind/worker/tool_authority.py',
    '    authority = ToolExecutionAuthority(claimed)\n',
    '    authority = ToolExecutionAuthority(claimed, deadline=deadline)\n')
replace_once('SKM/backend/src/skillmind/worker/executor.py',
    '                bind_tool_authority(claimed),\n',
    '                bind_tool_authority(\n'
    '                    claimed, deadline=asyncio.get_running_loop().time() + context.limits.wall_timeout_seconds\n'
    '                ),\n')
replace_once('SKM/backend/src/skillmind/worker/effects.py',
    '    async def execute(self, effect_execution_id: object, *, inline_parent: ClaimedRun | None = None) -> str:',
    '    @property\n'
    '    def wall_timeout_seconds(self) -> float:\n'
    '        """直接交付の経路選択へ既存の監督時間を読み取り専用で渡す。"""\n'
    '        return self._wall_timeout_seconds\n\n'
    '    async def execute(self, effect_execution_id: object, *, inline_parent: ClaimedRun | None = None) -> str:')
replace_once('SKM/backend/src/skillmind/worker/inline_effects.py',
    '        claimed = self._authority.claimed\n',
    '        claimed = self._authority.claimed\n'
    '        # 長い Run を一 Attempt に詰めて時間切れにしない。送信/提案作成の前に\n'
    '        # 既存 Effect 上限と交付余量を予約できなければ通常の継続へ戻す。\n'
    '        deadline = self._authority.deadline\n'
    '        if deadline is not None and (\n'
    '            deadline - asyncio.get_running_loop().time() <= self._executor.wall_timeout_seconds + 30\n'
    '        ):\n'
    '            return None\n')
p = Path('SKM/backend/tests/worker/test_inline_effects.py')
t = p.read_text()
t += '''

@pytest.mark.parametrize("remaining", [0, 300, 329])
async def test_low_attempt_time_selects_deferred_before_creating_effect(remaining):
    """最適化のために親期限を延長せず、元の通常継続へ未送信で戻す。"""
    h, s, e, a, p, f = fixture()
    e.wall_timeout_seconds = 300
    deadline = asyncio.get_running_loop().time() + remaining
    a.deadline = deadline
    assert await h.invoke({}, "call", str(uuid4())) is None
    assert a.deadline == deadline
    s.begin_inline_effect.assert_not_awaited()
    e.execute.assert_not_awaited()


async def test_inline_uses_remaining_attempt_time_without_resetting_it():
    """十分な原余量がある操作だけを実行し、次呼出しのために時刻をリセットしない。"""
    h, s, e, a, p, f = fixture()
    e.wall_timeout_seconds = 300
    deadline = asyncio.get_running_loop().time() + 600
    a.deadline = deadline
    result = await h.invoke({}, "call", str(uuid4()))
    assert result.proposal_id == p
    assert a.deadline == deadline
    e.execute.assert_awaited_once_with(f, inline_parent=a.claimed)
'''
p.write_text(t)
p = Path('docs/development/runtime-guide.md')
t = p.read_text()
t += '\n直接回执只在原 Attempt 剩余时间足够容纳既有 Effect 监督上限与交付余量时尝试；不足时在创建操作前走原延期路径，不延长或重置期限，也不因开启优化把长任务强行塞进一个 Attempt。\n'
p.write_text(t)
