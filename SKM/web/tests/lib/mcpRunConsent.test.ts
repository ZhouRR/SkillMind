import { describe, expect, it } from 'vitest'
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
