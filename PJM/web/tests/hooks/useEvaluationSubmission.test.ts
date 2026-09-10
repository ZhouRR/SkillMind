import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadEvaluationSubmission, submitEvaluation } from '../../src/api'
import { useEvaluationSubmission } from '../../src/hooks/useEvaluationSubmission'
import { EVALUATION_DATA as DATA, EVALUATION_SCOPE as S, SUBMISSION_KEY as KEY, evaluationReceipt, evaluationSubmissionInput } from '../fixtures/evaluation'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), submitEvaluation: vi.fn(), loadEvaluationSubmission: vi.fn() }))

const saved = vi.fn()
const expired = vi.fn()
/** 同じ owner を明示的に再描画する。実 DOM の mount/key は App browser で守る。 */
function render(overrides: Partial<Parameters<typeof useEvaluationSubmission>[0]> = {}) {
  hookPhases.cursor = 0
  return useEvaluationSubmission({ scope: S, csrfToken: 'synthetic-session', resultData: DATA, writable: true,
    onSaved: saved, onSessionExpired: expired, ...overrides })
}
/** 共有 query の state 更新と評価側の消費 effect を、timer を動かさず commit する。 */
async function settle(overrides: Partial<Parameters<typeof useEvaluationSubmission>[0]> = {}) {
  for (let index = 0; index < 4; index++) { await hookMicrotasks(); render(overrides); commitHooks() }
  return render(overrides)
}
/** Mock transport に渡された原 payload と完全一致する server 受付記録を返す。 */
function success() { vi.mocked(submitEvaluation).mockImplementation(async (_p, _r, input) => evaluationReceipt(input)) }

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
})
afterEach(() => { unmountHooks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('evaluation original request owner', () => {
  it('prevents same-tick duplicate POST and requires explicit new draft after success', async () => {
    success(); const hook = render(); commitHooks()
    hook.start(evaluationSubmissionInput()); hook.start(evaluationSubmissionInput())
    const done = await settle()
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
    expect(saved).toHaveBeenCalledTimes(1)
    expect(done.pending?.phase).toBe('confirmed')
    done.start(evaluationSubmissionInput()); await hookMicrotasks()
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
    expect(done.clear()).toBe(true)
    render().start(evaluationSubmissionInput()); await settle()
    expect(submitEvaluation).toHaveBeenCalledTimes(2)
    expect(vi.mocked(submitEvaluation).mock.calls[0]![2].submission_key).not.toBe(vi.mocked(submitEvaluation).mock.calls[1]![2].submission_key)
  })
  it('keeps original key/body through GET not-found, late publication, and explicit confirmation', async () => {
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('network'))
    const hook = render(); commitHooks(); const input = evaluationSubmissionInput(); hook.start(input)
    input.comment = 'edited'
    let current = await settle(); const original = current.pending!.request
    vi.mocked(loadEvaluationSubmission).mockRejectedValueOnce(new ApiProblemError('private', 404, 'evaluation_submission_not_found'))
    current.confirm(); current.confirm(); current.start(evaluationSubmissionInput()); current = await settle()
    expect(loadEvaluationSubmission).toHaveBeenCalledTimes(1)
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
    expect(current.pending).toMatchObject({ phase: 'unknown', uncertain: true, request: original, failure: { key: 'notSeen' } })
    vi.mocked(loadEvaluationSubmission).mockResolvedValue(evaluationReceipt(JSON.parse(original.body!) as ReturnType<typeof evaluationSubmissionInput>))
    current.confirm(); current = await settle()
    expect(current.pending?.phase).toBe('confirmed')
    expect(saved).toHaveBeenCalledTimes(1)
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
  })
  it('explicit retry uses identical payload and UUID without consulting draft or requiring GET 404', async () => {
    vi.mocked(submitEvaluation).mockRejectedValueOnce(new TypeError('network'))
      .mockImplementationOnce(async (_p, _r, input) => evaluationReceipt(input))
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput())
    const unknown = await settle(); const first = vi.mocked(submitEvaluation).mock.calls[0]!
    unknown.resend(); unknown.resend(); await settle()
    expect(vi.mocked(submitEvaluation).mock.calls[1]!.slice(0, 4)).toEqual(first.slice(0, 4))
    expect(loadEvaluationSubmission).not.toHaveBeenCalled()
    expect(saved).toHaveBeenCalledTimes(1)
  })
  it.each(['cancel', 'timeout'])('makes %s of POST unknown and ignores later success', async (mode) => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); await hookMicrotasks()
    if (mode === 'cancel') render().cancel(); else vi.advanceTimersByTime(30_000)
    expect(render().pending).toMatchObject({ phase: 'unknown', uncertain: true })
    response.resolve(evaluationReceipt()); await settle()
    expect(saved).not.toHaveBeenCalled()
    expect(vi.mocked(submitEvaluation).mock.calls[0]![4]?.aborted).toBe(true)
  })
  it('synchronously aborts GET on cancel before a same-tick late 401 can expire the current session', async () => {
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('network'))
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); let current = await settle()
    current.confirm(); current = await settle()
    current.cancel(); response.reject(new ApiProblemError('private', 401))
    await hookMicrotasks()
    expect(expired).not.toHaveBeenCalled()
    expect(vi.mocked(loadEvaluationSubmission).mock.calls[0]![4]?.aborted).toBe(true)
    expect((await settle()).pending?.phase).toBe('unknown')
    expect(saved).not.toHaveBeenCalled()
  })
  it.each(['POST', 'GET'] as const)('ignores delayed timer %s 401 after the absolute deadline', async (method) => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('network'))
    if (method === 'POST') vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    else vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); let current = await settle()
    if (method === 'GET') { current.confirm(); current = await settle() }
    vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401)); current = await settle()
    expect(expired).not.toHaveBeenCalled(); expect(saved).not.toHaveBeenCalled()
    expect(current.pending?.phase).toBe('unknown')
  })
  it('preserves an active same-scope POST through detail loading and tab refresh', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); await hookMicrotasks()
    render({ writable: false }); commitHooks()
    expect(vi.mocked(submitEvaluation).mock.calls[0]![4]?.aborted).toBe(false)
    response.resolve(evaluationReceipt(vi.mocked(submitEvaluation).mock.calls[0]![2]))
    await settle({ writable: false })
    expect(saved).toHaveBeenCalledTimes(1)
  })
  it.each(['actor', 'project', 'run', 'result', 'session'])('does not leak late old 401 after %s owner is replaced', async (scope) => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); await hookMicrotasks()
    unmountHooks()
    const other = '00000000-0000-4000-8000-000000000999'
    const changed = scope === 'session' ? { csrfToken: 'new-session' } : { scope: { ...S, [`${scope}Id`]: other } }
    render(changed); commitHooks(); response.reject(new ApiProblemError('private', 401)); await settle(changed)
    expect(expired).not.toHaveBeenCalled(); expect(saved).not.toHaveBeenCalled(); expect(render(changed).pending).toBeNull()
  })
  it.each(['invalidRequest', 'invalidRevision'] as const)('returns an initial %s refusal to editing but preserves a previous unknown', async (key) => {
    const error = new ApiProblemError('private', key === 'invalidRequest' ? 422 : 400,
      key === 'invalidRequest' ? 'invalid_evaluation_request' : 'invalid_evaluation_revision')
    vi.mocked(submitEvaluation).mockRejectedValueOnce(error).mockRejectedValueOnce(new TypeError('network')).mockRejectedValueOnce(error)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); let current = await settle()
    expect(current.pending).toMatchObject({ phase: 'refused', uncertain: false, failure: { key } })
    expect(current.clear()).toBe(true)
    render().start(evaluationSubmissionInput()); current = await settle(); current.resend(); current = await settle()
    expect(current.pending).toMatchObject({ phase: 'unknown', uncertain: true, failure: { key } })
    expect(current.clear()).toBe(false)
  })
  it('keeps a conflict unknown and cannot use a new key or edited draft as retry', async () => {
    vi.mocked(submitEvaluation).mockRejectedValue(new ApiProblemError('private', 409, 'evaluation_submission_conflict'))
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); const current = await settle()
    expect(current.pending).toMatchObject({ phase: 'unknown', uncertain: true })
    expect(current.clear()).toBe(false); current.start(evaluationSubmissionInput()); await hookMicrotasks()
    expect(submitEvaluation).toHaveBeenCalledTimes(1)
  })
  it('allows archived read-only original-key lookup, but never invents payload for replay', async () => {
    vi.mocked(loadEvaluationSubmission).mockResolvedValue(evaluationReceipt())
    const props = { writable: false, accessFailure: { key: 'projectArchived' as const } }
    const hook = render(props); commitHooks(); expect(hook.lookup(KEY)).toBe(true)
    const current = await settle(props)
    expect(current.pending).toMatchObject({ phase: 'confirmed', request: { body: null } })
    current.resend(); current.start(evaluationSubmissionInput()); await hookMicrotasks()
    expect(submitEvaluation).not.toHaveBeenCalled()
    expect(saved).toHaveBeenCalledTimes(1)
  })
  it('refuses a mismatched actor or original value even for a structurally valid receipt', async () => {
    vi.mocked(submitEvaluation).mockImplementation(async (_p, _r, input) => {
      const receipt = evaluationReceipt(input); receipt.evaluation.user_id = S.projectId; return receipt
    })
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); const current = await settle()
    expect(current.pending?.phase).toBe('unknown'); expect(saved).not.toHaveBeenCalled()
  })
  it('closes current POST on an explicit read refusal without letting its late 401 affect the session', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(submitEvaluation).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); await hookMicrotasks()
    render().rejectAccess({ key: 'forbidden' }); response.reject(new ApiProblemError('private', 401))
    const current = await settle(); current.resend(); current.confirm(); await hookMicrotasks()
    expect(current.pending).toMatchObject({ phase: 'unknown', uncertain: true })
    expect(expired).not.toHaveBeenCalled(); expect(saved).not.toHaveBeenCalled()
    expect(submitEvaluation).toHaveBeenCalledTimes(1); expect(loadEvaluationSubmission).not.toHaveBeenCalled()
  })

  it('notifies current-session GET 401 globally once even after its denial observer ran first', async () => {
    vi.mocked(loadEvaluationSubmission).mockRejectedValue(new ApiProblemError('private', 401))
    const hook = render(); commitHooks(); hook.lookup(KEY)
    const current = await settle()
    expect(expired).toHaveBeenCalledTimes(1)
    expect(current.denied).toEqual({ key: 'sessionExpired' })
    current.onReadSessionExpired(); current.confirm(); await settle()
    expect(expired).toHaveBeenCalledTimes(1)
    expect(loadEvaluationSubmission).toHaveBeenCalledTimes(1)
    expect(saved).not.toHaveBeenCalled()
  })

  it('does not suppress a history GET 401 notification after the shared read failure observer', () => {
    const hook = render(); commitHooks()
    hook.rejectAccess({ key: 'sessionExpired' })
    hook.onReadSessionExpired(); hook.onReadSessionExpired()
    expect(expired).toHaveBeenCalledTimes(1)
  })

  it.each(['forbidden', 'notFound', 'resultMismatch', 'resultUnavailable'] as const)(
    'cannot confirm late GET 200 after another current read reports %s', async (key) => {
      const response = deferred<ReturnType<typeof evaluationReceipt>>()
      vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
      const hook = render(); commitHooks(); hook.lookup(KEY); let current = await settle()
      current.rejectAccess({ key })
      response.resolve(evaluationReceipt()); current = await settle()
      expect(current.pending).toMatchObject({ phase: 'unknown', failure: { key } })
      expect(saved).not.toHaveBeenCalled()
      expect(vi.mocked(loadEvaluationSubmission).mock.calls[0]![4]?.aborted).toBe(true)
    },
  )

  it.each(['projectArchived', 'csrfRejected'] as const)('preserves read confirmation after write-only refusal %s', async (key) => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.lookup(KEY); let current = await settle()
    current.rejectAccess({ key }); response.resolve(evaluationReceipt()); current = await settle()
    expect(current.pending?.phase).toBe('confirmed')
    expect(saved).toHaveBeenCalledTimes(1)
    expect(submitEvaluation).not.toHaveBeenCalled()
  })

  it('can end a mistyped read-only lookup after 404 without clearing or inventing a POST', async () => {
    vi.mocked(loadEvaluationSubmission).mockRejectedValueOnce(new ApiProblemError('private', 404, 'evaluation_submission_not_found'))
      .mockResolvedValueOnce(evaluationReceipt())
    const hook = render(); commitHooks(); hook.lookup(S.projectId); let current = await settle()
    expect(current.pending).toMatchObject({ phase: 'unknown', request: { body: null } })
    current.closeLookup(); current = render(); expect(current.pending).toBeNull()
    expect(current.lookup(KEY)).toBe(true); current = await settle()
    expect(current.pending?.phase).toBe('confirmed')
    expect(submitEvaluation).not.toHaveBeenCalled()
    expect(loadEvaluationSubmission).toHaveBeenCalledTimes(2)
  })

  it('ending a GET-only lookup aborts synchronously and never leaks its late 401', async () => {
    const response = deferred<ReturnType<typeof evaluationReceipt>>()
    vi.mocked(loadEvaluationSubmission).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.lookup(KEY); const current = await settle()
    current.closeLookup(); response.reject(new ApiProblemError('private', 401)); await hookMicrotasks()
    expect(expired).not.toHaveBeenCalled(); expect(saved).not.toHaveBeenCalled()
    expect((await settle()).pending).toBeNull()
  })

  it('does not offer the GET-only exit to a real POST unknown, including during confirmation', async () => {
    vi.mocked(submitEvaluation).mockRejectedValue(new TypeError('network'))
    vi.mocked(loadEvaluationSubmission).mockReturnValue(deferred<ReturnType<typeof evaluationReceipt>>().promise)
    const hook = render(); commitHooks(); hook.start(evaluationSubmissionInput()); let current = await settle()
    const original = current.pending!.request
    current.closeLookup(); expect(render().pending!.request).toBe(original)
    current.confirm(); current = await settle(); current.closeLookup()
    expect(render().pending).toMatchObject({ phase: 'checking', request: original })
    expect(vi.mocked(loadEvaluationSubmission).mock.calls[0]![4]?.aborted).toBe(false)
  })
})
