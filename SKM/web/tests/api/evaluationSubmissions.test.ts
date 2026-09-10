import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadEvaluationPage, loadEvaluationSubmission, submitEvaluation } from '../../src/api'
import { isEvaluationSubmissionInput } from '../../src/api/evaluationSubmissions'
import receiptContract from '../../../contracts/examples/evaluation-submission.v1.json'
import pageContract from '../../../contracts/examples/evaluation-page.v1.json'
import inputContract from '../../../contracts/examples/evaluation-submission-request.v1.json'
import { EVALUATION_SCOPE as S, SUBMISSION_KEY as KEY, evaluationReceipt, evaluationSubmissionInput } from '../fixtures/evaluation'

const OTHER = '00000000-0000-4000-8000-000000000999'
const NIL = '00000000-0000-0000-0000-000000000000'
/** 所有する合成 HTTP だけを返し、Backend や環境設定を読まない。 */
function respond(value: unknown, status = 200) {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(value), { status }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}
afterEach(() => vi.unstubAllGlobals())

describe('evaluation submission API contracts', () => {
  it.each([200, 201])('accepts the exact original POST receipt at %s and sends explicit UUID/CSRF', async (status) => {
    const fetch = respond(evaluationReceipt(), status)
    const input = evaluationSubmissionInput()
    await expect(submitEvaluation(S.projectId, S.runId, input, 'synthetic-csrf')).resolves.toEqual(evaluationReceipt())
    expect(fetch).toHaveBeenCalledExactlyOnceWith(expect.stringContaining(`/projects/${S.projectId}/runs/${S.runId}/evaluation-submissions`),
      expect.objectContaining({ method: 'POST', credentials: 'same-origin', headers: expect.objectContaining({ 'X-CSRF-Token': 'synthetic-csrf' }), body: JSON.stringify(input) }))
  })
  it('consumes the exact shared published examples without editing them', async () => {
    respond(receiptContract, 201)
    await expect(submitEvaluation(receiptContract.project_id, receiptContract.run_id, { ...inputContract, verdict: 'partially_accurate' }, 'synthetic')).resolves.toEqual(receiptContract)
    respond(pageContract)
    await expect(loadEvaluationPage(pageContract.project_id, pageContract.run_id, pageContract.result_id)).resolves.toEqual(pageContract)
  })
  it('confirmation only performs GET with the original result id and key', async () => {
    const fetch = respond(evaluationReceipt())
    await expect(loadEvaluationSubmission(S.projectId, S.runId, S.resultId, KEY)).resolves.toEqual(evaluationReceipt())
    expect(fetch).toHaveBeenCalledExactlyOnceWith(expect.stringContaining(`/evaluation-submissions/${KEY}?result_id=${S.resultId}`),
      expect.objectContaining({ cache: 'no-store', credentials: 'same-origin' }))
    expect(fetch.mock.calls[0]![1]).not.toHaveProperty('body')
  })
  it.each([202, 204, 206])('does not call unexpected successful status %s a saved receipt', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(status === 204 ? null : JSON.stringify(evaluationReceipt()), { status })))
    await expect(submitEvaluation(S.projectId, S.runId, evaluationSubmissionInput(), 'synthetic')).rejects.toThrow()
  })
  it.each(['project', 'run', 'key', 'result', 'record-run', 'extra', 'record-extra', 'missing', 'null-original', 'date', 'nil', 'rating', 'comment', 'revision'])('rejects wrong receipt %s', async (mode) => {
    const body = evaluationReceipt()
    if (mode === 'project') body.project_id = OTHER
    if (mode === 'run') body.run_id = OTHER
    if (mode === 'key') body.submission_key = OTHER
    if (mode === 'result') body.evaluation.result_id = OTHER
    if (mode === 'record-run') body.evaluation.run_id = OTHER
    if (mode === 'extra') Object.assign(body, { private: true })
    if (mode === 'record-extra') Object.assign(body.evaluation, { private: true })
    if (mode === 'missing') Reflect.deleteProperty(body.evaluation, 'user_id')
    if (mode === 'null-original') Reflect.deleteProperty(body.evaluation.revisions[0]!, 'original_value')
    if (mode === 'date') body.evaluation.created_at = '2026-02-30T00:00:00Z'
    if (mode === 'nil') body.evaluation.evaluation_id = NIL
    if (mode === 'rating') body.evaluation.rating = 4
    if (mode === 'comment') body.evaluation.comment = 'another submission'
    if (mode === 'revision') body.evaluation.revisions[0]!.suggested_value = 'not original input'
    respond(body, 201)
    await expect(submitEvaluation(S.projectId, S.runId, evaluationSubmissionInput(), 'synthetic')).rejects.toThrow()
  })
  it.each(['key', 'result', 'extra', 'nan', 'surrogate', 'duplicate', 'escape', 'many', 'missing', 'getter'])('rejects invalid input before any HTTP: %s', async (mode) => {
    const body = evaluationSubmissionInput()
    if (mode === 'key') body.submission_key = NIL
    if (mode === 'result') body.result_id = 'bad'
    if (mode === 'extra') Object.assign(body, { original_value: null })
    if (mode === 'nan') body.revisions[0]!.suggested_value = NaN
    if (mode === 'surrogate') body.comment = '\ud800'
    if (mode === 'duplicate') body.revisions.push(structuredClone(body.revisions[0]!))
    if (mode === 'escape') body.revisions[0]!.pointer = '/bad~3'
    if (mode === 'many') body.revisions = Array.from({ length: 101 }, (_, index) => ({ pointer: `/a${index}`, suggested_value: null, reason: 'ok' }))
    if (mode === 'missing') Reflect.deleteProperty(body.revisions[0]!, 'suggested_value')
    if (mode === 'getter') Object.defineProperty(body.revisions[0], 'suggested_value', { enumerable: true, get: () => null })
    const fetch = respond(evaluationReceipt())
    expect(isEvaluationSubmissionInput(body)).toBe(false)
    await expect(submitEvaluation(S.projectId, S.runId, body, 'synthetic')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })
  it('takes an immutable copy of input before awaiting a POST', async () => {
    const body = evaluationSubmissionInput()
    const receipt = evaluationReceipt(body)
    let release!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>((resolve) => { release = resolve })))
    const promise = submitEvaluation(S.projectId, S.runId, body, 'synthetic')
    body.comment = 'edited'; body.revisions[0]!.suggested_value = 'edited'
    release(new Response(JSON.stringify(receipt), { status: 201 }))
    await expect(promise).resolves.toEqual(receipt)
  })
  it.each([401, 403, 404, 409, 503])('preserves typed Problem status %s for controlled UI classification', async (status) => {
    respond({ status, code: 'evaluation_submission_not_found', detail: 'must not be rendered' }, status)
    await expect(loadEvaluationSubmission(S.projectId, S.runId, S.resultId, KEY)).rejects.toMatchObject({ status, code: 'evaluation_submission_not_found' })
  })
  it('rejects a late response when its signal was cancelled even if fetch ignores abort', async () => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => { controller.abort(); return Promise.resolve(new Response(JSON.stringify(evaluationReceipt()))) }))
    await expect(loadEvaluationSubmission(S.projectId, S.runId, S.resultId, KEY, controller.signal)).rejects.toThrow()
  })
})

describe('evaluation server-cursor page API', () => {
  it('accepts an empty last page and includes only explicit limit/after', async () => {
    const fetch = respond({ project_id: S.projectId, run_id: S.runId, result_id: S.resultId, items: [], next_cursor: null })
    await expect(loadEvaluationPage(S.projectId, S.runId, S.resultId, OTHER, 20)).resolves.toMatchObject({ items: [], next_cursor: null })
    expect(fetch.mock.calls[0]![0]).toContain(`/evaluations/page?limit=20&after=${OTHER}`)
  })
  it.each(['project', 'run', 'result', 'item-result', 'extra', 'missing', 'duplicate', 'order', 'cursor', 'repeated', 'oversize'])('rejects invalid page %s', async (mode) => {
    const first = evaluationReceipt().evaluation
    const second = { ...first, evaluation_id: OTHER, created_at: '2026-09-10T01:00:00.000002Z' }
    const body = { project_id: S.projectId, run_id: S.runId, result_id: S.resultId, items: [first, second], next_cursor: null as string | null }
    if (mode === 'project') body.project_id = OTHER
    if (mode === 'run') body.run_id = OTHER
    if (mode === 'result') body.result_id = OTHER
    if (mode === 'item-result') first.result_id = OTHER
    if (mode === 'extra') Object.assign(body, { total: 100 })
    if (mode === 'missing') Reflect.deleteProperty(body, 'next_cursor')
    if (mode === 'duplicate') second.evaluation_id = first.evaluation_id
    if (mode === 'order') body.items.reverse()
    if (mode === 'cursor') body.next_cursor = first.evaluation_id
    if (mode === 'repeated') body.next_cursor = NIL
    if (mode === 'oversize') body.items = Array.from({ length: 21 }, () => first)
    respond(body)
    await expect(loadEvaluationPage(S.projectId, S.runId, S.resultId)).rejects.toThrow()
  })
  it.each([0, 101, 1.5])('rejects invalid requested limit %s without fetching', async (limit) => {
    const fetch = respond({})
    await expect(loadEvaluationPage(S.projectId, S.runId, S.resultId, null, limit)).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })
  it('retains invalid-cursor Problem rather than falling back to the old unpaged endpoint', async () => {
    const fetch = respond({ status: 400, code: 'invalid_evaluation_cursor' }, 400)
    await expect(loadEvaluationPage(S.projectId, S.runId, S.resultId, OTHER)).rejects.toBeInstanceOf(ApiProblemError)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
