import { describe, expect, it } from 'vitest'
import { ApiProblemError } from '../../src/api'
import { evaluationFailure, evaluationInput, evaluationOriginal, evaluationPayload,
  freezeEvaluationSubmission, matchesEvaluationReceipt, mergeEvaluationRecords, type EvaluationDraft } from '../../src/lib/evaluationSubmission'
import { isJsonValue } from '../../src/lib/jsonValue'
import { EVALUATION_DATA as DATA, EVALUATION_SCOPE as S, SUBMISSION_KEY as KEY, evaluationReceipt, evaluationSubmissionInput } from '../fixtures/evaluation'

/** 全項目が memory-only の独立草稿を返す。 */
function draft(): EvaluationDraft {
  return { rating: 3, verdict: 'uncertain', comment: ' Original whitespace ', revisions: [
    { id: 0, pointer: '/nullable', value: 'null', reason: 'Keep null' },
    { id: 1, pointer: '/a~1b/~0value', value: '{"b":null,"a":1}', reason: 'Escaped target' },
  ] }
}
describe('evaluation draft and immutable original request', () => {
  it('accepts null and escaped pointers and does not change original Result or trim input', () => {
    const data = structuredClone(DATA)
    const checked = evaluationInput(draft(), data)
    expect(checked.error).toBeNull()
    expect(checked.input).toMatchObject({ comment: ' Original whitespace ', revisions: [
      { pointer: '/nullable', suggested_value: null }, { pointer: '/a~1b/~0value', suggested_value: { a: 1, b: null } },
    ] })
    expect(data).toEqual(DATA)
    expect(evaluationOriginal(data, '/nullable')).toEqual({ found: true, value: null })
  })
  it.each(['', '/missing', '/array/01', '/array/-', '/array/2', '/array/0/value', '/summary/a', '/bad~2', '/bad~', '/__proto__'])('rejects nonexistent or malformed target %s', (pointer) => {
    expect(evaluationOriginal(DATA, pointer).found).toBe(false)
    const input = draft(); input.revisions[0]!.pointer = pointer
    expect(evaluationInput(input, DATA)).toMatchObject({ input: null, error: { key: 'invalidPointer', row: 0 } })
  })
  it.each(['bare string', '', '{', 'undefined', 'NaN', '1e400', '"\\ud800"'])('does not silently convert invalid JSON %s into text', (value) => {
    const input = draft(); input.revisions[0]!.value = value
    expect(evaluationInput(input, DATA)).toMatchObject({ input: null, error: { key: 'invalidJson', row: 0 } })
  })
  it('rejects the whole draft for duplicate paths without sending a partial revision list', () => {
    const input = draft(); input.revisions[1]!.pointer = '/nullable'
    expect(evaluationInput(input, DATA)).toMatchObject({ input: null, error: { key: 'duplicatePointer', row: 1 } })
  })
  it.each([0, 1001])('requires bounded nonempty revision reason %s', (length) => {
    const input = draft(); input.revisions[0]!.reason = 'a'.repeat(length)
    expect(evaluationInput(input, DATA).error?.key).toBe('invalidReason')
  })
  it('accepts 100 distinct revisions and rejects 101', () => {
    const data = Object.fromEntries(Array.from({ length: 101 }, (_, index) => [`a${index}`, null]))
    const input = draft()
    input.revisions = Array.from({ length: 100 }, (_, id) => ({ id, pointer: `/a${id}`, value: 'null', reason: 'ok' }))
    expect(evaluationInput(input, data).error).toBeNull()
    input.revisions.push({ id: 100, pointer: '/a100', value: 'null', reason: 'ok' })
    expect(evaluationInput(input, data).error?.key).toBe('invalidDraft')
  })
  it('freezes exact body and Result before any await and returns a new transport copy', () => {
    const input = evaluationSubmissionInput(); const data = structuredClone(DATA)
    const frozen = freezeEvaluationSubmission(S, KEY, data, input)
    input.comment = 'edited'; input.revisions[0]!.suggested_value = 99; data.nullable = null
    const first = evaluationPayload(frozen); first.revisions[0]!.reason = 'transport mutation'
    expect(evaluationPayload(frozen)).toEqual(evaluationSubmissionInput())
    expect(Object.isFrozen(frozen)).toBe(true)
    expect(Object.isFrozen(frozen.scope)).toBe(true)
    expect(frozen).not.toHaveProperty('csrfToken')
  })
  it('permits a GET-only original key without inventing a POST payload', () => {
    const original = freezeEvaluationSubmission(S, KEY, DATA, null)
    expect(() => evaluationPayload(original)).toThrow()
    expect(matchesEvaluationReceipt(evaluationReceipt(), original)).toBe(true)
  })
  it.each(['actor', 'project', 'run', 'result', 'key', 'rating', 'comment', 'value', 'original', 'pointer', 'reason'])('does not confirm another receipt: %s', (mode) => {
    const original = freezeEvaluationSubmission(S, KEY, DATA, evaluationSubmissionInput())
    const receipt = evaluationReceipt(); const other = '00000000-0000-4000-8000-000000000999'
    if (mode === 'actor') receipt.evaluation.user_id = other
    if (mode === 'project') receipt.project_id = other
    if (mode === 'run') receipt.evaluation.run_id = other
    if (mode === 'result') receipt.evaluation.result_id = other
    if (mode === 'key') receipt.submission_key = other
    if (mode === 'rating') receipt.evaluation.rating = 1
    if (mode === 'comment') receipt.evaluation.comment = 'changed'
    if (mode === 'value') receipt.evaluation.revisions[0]!.suggested_value = false
    if (mode === 'original') receipt.evaluation.revisions[0]!.original_value = 'not null'
    if (mode === 'pointer') receipt.evaluation.revisions[0]!.pointer = '/summary'
    if (mode === 'reason') receipt.evaluation.revisions[0]!.reason = 'changed'
    expect(matchesEvaluationReceipt(receipt, original)).toBe(false)
  })
  it('ignores only JSON object key order, not revision array order or boolean-number differences', () => {
    const checked = evaluationInput(draft(), DATA).input!
    const original = freezeEvaluationSubmission(S, KEY, DATA, checked)
    const receipt = evaluationReceipt({ ...checked, submission_key: KEY, result_id: S.resultId })
    receipt.evaluation.revisions[1]!.original_value = { a: 1, b: null }
    receipt.evaluation.revisions[1]!.suggested_value = { a: 1, b: null }
    expect(matchesEvaluationReceipt(receipt, original)).toBe(true)
    receipt.evaluation.revisions.reverse()
    expect(matchesEvaluationReceipt(receipt, original)).toBe(false)
  })
  it('keeps a confirmed receipt while appending an older page and refuses conflicting immutable IDs', () => {
    const saved = evaluationReceipt().evaluation
    const earlier = { ...saved, evaluation_id: '00000000-0000-4000-8000-000000000079' }
    expect(mergeEvaluationRecords([saved], [earlier, structuredClone(saved)])).toEqual([saved, earlier])
    expect(() => mergeEvaluationRecords([saved], [{ ...saved, comment: 'contradiction' }])).toThrow()
  })
  it.each([NaN, Infinity, undefined, new Date(), () => 1, 1n, '\ud800'])('does not JSON-coerce unsafe value %s', (value) => expect(isJsonValue(value)).toBe(false))
})

describe('controlled evaluation Problem feedback', () => {
  it.each([
    [400, 'invalid_evaluation_revision', 'invalidRevision'], [422, 'invalid_evaluation_request', 'invalidRequest'],
    [422, 'validation_error', 'invalidRequest'], [409, 'evaluation_submission_conflict', 'conflict'],
    [409, 'evaluation_result_mismatch', 'resultMismatch'], [409, 'result_not_available', 'resultUnavailable'],
    [409, 'project_archived', 'projectArchived'], [401, '', 'sessionExpired'], [403, 'csrf_rejected', 'csrfRejected'],
    [403, '', 'forbidden'], [404, 'run_not_found', 'notFound'], [404, 'project_not_found', 'notFound'],
    [404, 'evaluation_submission_not_found', 'notSeen'], [400, 'invalid_evaluation_cursor', 'cursorInvalid'],
  ] as const)('maps exact %s %s to %s without rendering details', (status, code, key) => {
    expect(evaluationFailure(new ApiProblemError('private detail', status, code), true)).toEqual({ key })
  })
  it.each([200, 201, 202, 400, 422, 500, 503])('keeps unspecified write failure %s unknown', (status) => {
    expect(evaluationFailure(new ApiProblemError('private', status, 'unrecognized'), true)).toEqual({ key: 'unknown' })
  })
})
