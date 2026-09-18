"""確認済み UI 文言、局所参照の上限と回帰 fixture を同じ patch へ補う。"""
from __future__ import annotations

import json
import os
from pathlib import Path

STATE=Path(os.environ['RUNNER_TEMP'])/'generic-mcp-paths.json'
paths=set(json.loads(STATE.read_text()))


def edit(path, old, new):
    """一意の確認済み anchor 以外を変更しない。"""
    p=Path(path)
    t=p.read_text(encoding='utf-8')
    assert t.count(old)==1, (path,old[:100],t.count(old))
    p.write_text(t.replace(old,new,1),encoding='utf-8')
    paths.add(path)


edit('SKM/backend/tests/runs/test_realtime_buffer.py', 'for sequence in range(1000):', 'for sequence in range(1, 1001):')
edit('SKM/backend/tests/runs/test_realtime_buffer.py', '    sink.publish.side_effect = lambda _: blocked.wait()\n', '')
edit('SKM/backend/tests/agent/test_mcp_tools_provider.py',
     '    assert not resolve_effect_capability("mcp.call/v1").supports_run_approval("mcp")',
     '    assert resolve_effect_capability("mcp.call/v1").supports_run_approval("mcp")\n    assert not resolve_effect_capability("mcp.call/v1").supports_run_approval("other")\n    assert not resolve_effect_capability("mcp.call/v1").supports_run_approval(None)')
edit('SKM/backend/tests/agent/test_mcp_tools_provider.py',
     'def test_profile_scope_requires_readback_and_cannot_reuse_autoapproval():',
     'def test_profile_scope_requires_readback_and_only_registered_mcp_run_consent():')
edit('SKM/backend/tests/runs/test_mcp_run_approval.py',
     'async def test_mcp_run_start_approval_keeps_current_authority_checks(',
     'async def test_extended_consent_keeps_existing_current_authority_checks(')

labels={
    'zh': ('自动批准（数据库写入、文档保存、Git 提交）', '自动批准（数据库写入、文档保存、Git 提交、MCP 操作）'),
    'ja': ('自動承認（データベース・文書保存・Git 提出）', '自動承認（データベース・文書保存・Git 提出・MCP 操作）'),
    'en': ('Automatically approve database writes, document saves and Git commits', 'Automatically approve database writes, document saves, Git commits and MCP operations'),
}
for language,(old,new) in labels.items():
    edit(f'SKM/web/src/lib/i18n/{language}.ts', old, new)

p='SKM/backend/src/skillmind/integrations/mcp_schema.py'
edit(p, '    visited: set[int] = set()\n', '    visited_nodes = 0\n')
edit(p, '        if isinstance(value, bool):\n', '        nonlocal visited_nodes\n        visited_nodes += 1\n        if visited_nodes > 100_000:\n            raise ValueError("MCP schema reference expansion exceeds its limit")\n        if isinstance(value, bool):\n')
edit(p, '        if identity in visited:\n            return\n', '')
edit(p, '        visited.add(identity)\n', '')
edit(p, '''    cls = validator_for(schema, default=Draft202012Validator)
    if isinstance(schema, Mapping) and "$schema" in schema:
        cls = validator_for(schema, default=None)
        if cls is None:
            raise ValueError("MCP schema dialect is not supported")
''', '''    cls = validator_for(
        schema, default=None if isinstance(schema, Mapping) and "$schema" in schema
        else Draft202012Validator,
    )
    if cls is None:
        raise ValueError("MCP schema dialect is not supported")
''')

p=Path('SKM/web/tests/lib/mcpRunConsent.test.ts')
assert not p.exists()
p.write_text('''import { describe, expect, it } from 'vitest'
import { freezeRunSubmission, submissionPayload } from '../../src/lib/runSubmission'
import type { TaskDraft } from '../../src/lib/taskDraft'

/** 同じ明示 checkbox を原要求に固定し、再送で新たな同意を追加しない。 */
describe('Run-scoped MCP consent', () => {
  const scope = { actorId: 'actor', projectId: 'project' }
  const draft: TaskDraft = { skillVersionId: 'version', taskKey: 'execute', taskTitle: 'Task', input: {}, sources: {} }

  it.each([true, false])('freezes explicit consent %s without adding another step', (enabled) => {
    const request = freezeRunSubmission(scope, { ...draft, autoApprove: enabled }, 'generic', 'original-key')
    expect(submissionPayload(request)).toMatchObject({
      auto_approve: enabled, auto_approve_git: enabled, auto_approve_mcp: enabled,
    })
  })

  it('does not infer consent when the option was not provided', () => {
    const request = freezeRunSubmission(scope, draft, 'generic', 'manual-key')
    expect(submissionPayload(request)).not.toHaveProperty('auto_approve_mcp')
  })

  it('replays the frozen body, not an edited draft or current defaults', () => {
    const current = { ...draft, autoApprove: false }
    const request = freezeRunSubmission(scope, current, 'generic', 'original-key')
    current.autoApprove = true
    expect(submissionPayload(request).auto_approve_mcp).toBe(false)
    const original = { ...request, body: JSON.stringify({ skill_version_id: 'version', task_key: 'execute', input: {}, sources: {}, auto_approve: true, auto_approve_git: true }) }
    expect(submissionPayload(original)).not.toHaveProperty('auto_approve_mcp')
  })
})
''',encoding='utf-8')
paths.add(str(p))

p=Path('SKM/backend/tests/agent/test_mcp_hot_path.py')
t=p.read_text(encoding='utf-8')
t+='''\n\ndef test_repeated_reference_graph_cannot_expand_without_a_bound():
    """小さい入力の指数参照を compile 時に止め、live tool 実行へ持ち込まない。"""
    definitions = {"leaf": {"type": "integer"}}
    previous = "leaf"
    for level in range(5):
        name = f"level_{level}"
        definitions[name] = {"allOf": [{"$ref": f"#/$defs/{previous}"}] * 20}
        previous = name
    with pytest.raises(ValueError):
        mcp_schema.validate_schema({"$defs": definitions, "$ref": f"#/$defs/{previous}"})


def test_local_reference_readback_is_verified_at_runtime_without_false_static_rejection():
    """部分射影で参照を誤解しないが、全出力と実 read-back の違反は拒否する。"""
    from skillmind.effects.mcp_call import check_read_back
    from skillmind.integrations.mcp_readback import McpReadBackError, validate_read_back_schema

    schema = {"$defs": {"state": {"enum": ["ready", "pending"]}}, "type": "object",
              "properties": {"state": {"$ref": "#/$defs/state"}}, "required": ["state"]}
    checks = [{"path": "/state", "equals": "ready"}]
    validate_read_back_schema(schema, checks)
    mcp_tools.validate_tool_value(schema, {"state": "ready"})
    with pytest.raises(ValueError):
        mcp_tools.validate_tool_value(schema, {"state": "invented"})
    with pytest.raises(McpReadBackError):
        check_read_back({"checks": checks}, {"state": "pending"})
'''
p.write_text(t,encoding='utf-8')

for p in Path('docs').rglob('*.md'):
    for line in p.read_text(encoding='utf-8').splitlines():
        if 'MCP' in line and ('自动批准' in line or 'Schema' in line or '逐次' in line):
            print('MCP_DOC_REVIEW',str(p),line)
STATE.write_text(json.dumps(sorted(paths)),encoding='utf-8')
print('PATCH_PATHS',json.dumps(sorted(paths)))
