import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import {
  canStartRunSubmission,
  failedRunSubmission,
  freezeRunSubmission,
  sendingRunSubmission,
  submissionBelongsTo,
  submissionPayload,
  type RunSubmissionScope,
} from '../../src/lib/runSubmission'
import type { TaskDraft } from '../../src/lib/taskDraft'

const scope: RunSubmissionScope = { actorId: 'actor-1', projectId: 'project-1' }

/** 明示の集合と順序を持つ業務入力で、画面編集中の drift を検出する。 */
function draft(): TaskDraft {
  return {
    skillVersionId: 'version-1', taskKey: 'analyze', taskTitle: 'Analysis',
    input: { subject: 'original', positions: [2, 1] }, sources: { docs: 'project-documents:all' },
  }
}

/** 固定 key はテストだけの identity。実 UI の新規送信は共通 Web Crypto helper を使う。 */
function original() {
  return freezeRunSubmission(scope, draft(), 'analysis/v1', 'original-key')
}

describe('frozen Run submission', () => {
  it('keeps the original request when the draft or a decoded copy is edited', () => {
    // 再送は現在の form も過去の API 呼出し object も読まない。
    const form = draft()
    const saved = freezeRunSubmission(scope, form, 'analysis/v1', 'original-key')
    form.input.subject = 'edited'
    form.input.positions = [1, 2]
    form.sources.docs = 'document:new'
    const first = submissionPayload(saved)
    first.input.subject = 'mutated'
    first.sources.docs = 'document:other'
    expect(submissionPayload(saved)).toEqual({
      skill_version_id: 'version-1', task_key: 'analyze',
      input: { subject: 'original', positions: [2, 1] }, sources: { docs: 'project-documents:all' },
    })
    expect(Object.isFrozen(saved)).toBe(true)
    expect(saved.idempotencyKey).toBe('original-key')
  })

  it('retains omitted choices and does not implement server normalization', () => {
    // default の展開と文書集合の正規化は server の責務。
    const form = draft()
    form.sources = {}
    expect(submissionPayload(freezeRunSubmission(scope, form, 'analysis/v1', 'key')).sources).toEqual({})
  })

  it.each([{ actorId: '', projectId: 'project' }, { actorId: 'actor', projectId: '' }])(
    'does not create or send without an authenticated scope %j', (invalidScope) => {
      expect(() => freezeRunSubmission(invalidScope, draft(), 'analysis/v1', 'key')).toThrow()
      expect(canStartRunSubmission(null, invalidScope, true)).toBe(false)
    },
  )

  it('does not replay under a different actor or project', () => {
    const saved = original()
    expect(submissionBelongsTo(saved, scope)).toBe(true)
    expect(submissionBelongsTo(saved, { ...scope, actorId: 'other' })).toBe(false)
    expect(submissionBelongsTo(saved, { ...scope, projectId: 'other' })).toBe(false)
  })
})

describe('creation uncertainty and explicit replacement', () => {
  it.each([
    new TypeError('fetch failed'), new Error('invalid Run response'),
    new ApiProblemError('non JSON', 201), new ApiProblemError('timeout', 408),
    new ApiProblemError('throttled', 429), new ApiProblemError('gateway', 502), undefined,
  ])('keeps the original key after an unconfirmed result: %j', (error) => {
    const sent = sendingRunSubmission(original())
    const failed = failedRunSubmission(sent, error)
    expect(failed.phase).toBe('unknown')
    expect(failed.mayHaveCreated).toBe(true)
    expect(failed.request).toBe(sent.request)
    expect(canStartRunSubmission(failed, scope, false)).toBe(false)
    expect(canStartRunSubmission(failed, scope, true)).toBe(true)
  })

  it.each([401, 403, 404, 422])('retains an original request after a %i refusal', (status) => {
    const failed = failedRunSubmission(sendingRunSubmission(original()), new ApiProblemError('rejected', status))
    expect(failed.phase).toBe('rejected')
    expect(failed.mayHaveCreated).toBe(false)
    expect(canStartRunSubmission(failed, scope, false)).toBe(false)
  })

  it('does not erase earlier uncertainty when a later retry is rejected', () => {
    const unknown = failedRunSubmission(sendingRunSubmission(original()), new TypeError('offline'))
    const retried = sendingRunSubmission(unknown.request, unknown)
    const refused = failedRunSubmission(retried, new ApiProblemError('access revoked', 403))
    expect(refused.phase).toBe('rejected')
    expect(refused.mayHaveCreated).toBe(true)
    expect(refused.request).toBe(unknown.request)
  })

  it('requires a deliberate new request after a conflict and never grants it while sending', () => {
    const sent = sendingRunSubmission(original())
    const conflict = failedRunSubmission(sent, new ApiProblemError('ambiguous history', 409, 'idempotency_conflict'))
    expect(conflict.phase).toBe('conflict')
    expect(canStartRunSubmission(conflict, scope, false)).toBe(false)
    expect(canStartRunSubmission(conflict, scope, true)).toBe(true)
    expect(canStartRunSubmission(sent, scope, true)).toBe(false)
    expect(canStartRunSubmission(conflict, { ...scope, actorId: 'other' }, true)).toBe(false)
  })
})
