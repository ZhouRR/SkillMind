import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { applyInteractionSnapshot, canConfirmInteraction, freezeInteractionResponse, hasDuplicateChoiceOptionKeys, interactionAnswer,
  interactionAccessFailure, interactionFailure, interactionPayload, type PendingInteractionResponse } from '../../src/lib/interactionResponse'
import { INTERACTION_SCOPE, interactionFixture, interactionReceipt, interactionRun } from '../fixtures/interaction'

describe('ordinary answer drafts and immutable identity', () => {
  it('requires an explicit answer even when recommended or not required', () => {
    expect(interactionAnswer(interactionFixture('CHOICE'), '', [])).toBeNull()
    expect(interactionAnswer(interactionFixture(), '  ', [])).toBeNull()
    expect(interactionAnswer(interactionFixture('REVIEW'), 'review', [])).toEqual({ text: 'review' })
    expect(interactionAnswer(interactionFixture('EFFECT_APPROVAL'), 'approve', ['a'])).toBeNull()
  })

  it('checks exact choice keys, uniqueness, cardinality and retains chosen order', () => {
    const question = interactionFixture('CHOICE')
    for (const choices of [['foreign'], ['a', 'a'], ['a', 'b']]) expect(interactionAnswer(question, '', choices)).toBeNull()
    expect(interactionAnswer({ ...question, prompt: { ...question.prompt, allow_multiple: true } }, ' note ', ['b', 'a']))
      .toEqual({ selected_option_keys: ['b', 'a'], text: 'note' })
  })

  it('counts Unicode code points without truncating the draft', () => {
    const boundary = '😀'.repeat(10_000)
    expect(interactionAnswer(interactionFixture(), boundary, [])).toEqual({ text: boundary })
    expect(interactionAnswer(interactionFixture(), `${boundary}x`, [])).toBeNull()
  })

  it('rejects new answers to duplicate choice keys without rewriting historical options', () => {
    const question = interactionFixture('CHOICE')
    question.options.push({ key: 'a', label: 'A different historical meaning' })
    const original = structuredClone(question)
    expect(hasDuplicateChoiceOptionKeys(question)).toBe(true)
    expect(interactionAnswer(question, 'explicit note', ['a'])).toBeNull()
    expect(interactionAnswer(question, 'an unambiguous other option', ['b'])).toBeNull()
    expect(question).toEqual(original)
    expect(hasDuplicateChoiceOptionKeys(interactionFixture('CHOICE'))).toBe(false)
    expect(hasDuplicateChoiceOptionKeys({ ...question, interaction_type: 'REVIEW' })).toBe(false)
  })

  it('freezes original actor target version body and key against later edits', () => {
    const question = interactionFixture('CHOICE')
    const draft = { selected_option_keys: ['b', 'a'], text: 'original' }
    const request = freezeInteractionResponse(INTERACTION_SCOPE, question, draft, 'original-key')
    question.version = 9
    draft.selected_option_keys.reverse()
    draft.text = 'edited'
    interactionPayload(request).selected_option_keys?.push('foreign')
    expect(request).toMatchObject({ ...INTERACTION_SCOPE, version: 3, idempotencyKey: 'original-key' })
    expect(interactionPayload(request)).toEqual({ selected_option_keys: ['b', 'a'], text: 'original' })
    expect(Object.isFrozen(request)).toBe(true)
  })
})

describe('ordinary reply failures and current snapshots', () => {
  it.each([
    [401, undefined, 'sessionExpired'], [403, 'csrf_rejected', 'csrfRejected'], [409, 'project_archived', 'projectArchived'],
    [403, undefined, 'forbidden'], [404, 'run_not_found', 'notFound'], [404, undefined, 'unknown'],
    [409, 'interaction_conflict', 'conflict'], [409, undefined, 'unknown'], [410, 'interaction_expired', 'expired'],
    [410, undefined, 'unknown'], [422, 'interaction_response_invalid', 'invalidAnswer'], [422, 'validation_error', 'invalidAnswer'],
    [422, undefined, 'unknown'], [500, undefined, 'unknown'],
  ] as const)('uses safe classification for HTTP %s / %s', (status, code, key) => {
    expect(interactionFailure(new ApiProblemError('private server body', status, code), true)).toEqual({ key })
  })

  it('does not turn transport/JSON failure into a known rejection', () => {
    for (const error of [new TypeError('private'), new SyntaxError('private'), new DOMException('aborted', 'AbortError')]) {
      expect(interactionFailure(error, true)).toEqual({ key: 'unknown' })
      expect(interactionFailure(error, false)).toEqual({ key: 'loadFailed' })
    }
  })

  it('separates Workspace access refusals from transient read failures', () => {
    for (const key of ['sessionExpired', 'csrfRejected', 'projectArchived', 'forbidden', 'notFound'] as const) {
      expect(interactionAccessFailure({ key })).toEqual({ key })
    }
    for (const key of ['loadFailed', 'unknown', 'conflict', 'expired', 'invalidAnswer'] as const) {
      expect(interactionAccessFailure({ key })).toBeNull()
    }
    expect(interactionAccessFailure(null)).toBeNull()
    expect(interactionAccessFailure(undefined)).toBeNull()
  })

  it('rejects old or unrelated snapshots without changing the current Run', () => {
    const current = { ...interactionRun(), status: 'SUCCEEDED' as const, row_version: 10 }
    expect(applyInteractionSnapshot(current, interactionReceipt())).toBe(current)
    expect(applyInteractionSnapshot(interactionRun(), { ...interactionReceipt(), project_id: 'other' })).toEqual(interactionRun())
    expect(applyInteractionSnapshot(interactionRun(), interactionReceipt())).toMatchObject({ status: 'QUEUED', row_version: 6 })
  })

  it('never enables original confirmation after an identity or permission rejection', () => {
    const pending: PendingInteractionResponse = { request: freezeInteractionResponse(INTERACTION_SCOPE, interactionFixture(), { text: 'original' }, 'key'),
      phase: 'unknown', uncertain: true, failure: null, receipt: null }
    expect(canConfirmInteraction(pending)).toBe(true)
    for (const key of ['sessionExpired', 'csrfRejected', 'forbidden', 'projectArchived', 'notFound'] as const) {
      expect(canConfirmInteraction({ ...pending, phase: 'rejected', failure: { key } })).toBe(false)
    }
  })
})
