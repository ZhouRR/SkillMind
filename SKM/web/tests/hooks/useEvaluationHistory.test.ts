import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadEvaluationPage, type EvaluationPage, type EvaluationRecord } from '../../src/api'
import { useEvaluationHistory } from '../../src/hooks/useEvaluationHistory'
import { EVALUATION_SCOPE as S, evaluationReceipt } from '../fixtures/evaluation'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), loadEvaluationPage: vi.fn() }))

const expired = vi.fn()
const failed = vi.fn()

/** 評価 ID と時刻を独立に指定し、配列位置を server cursor の代用にしない。 */
function record(index: number, createdAt = '2026-09-10T01:00:00.000001Z'): EvaluationRecord {
  return { ...evaluationReceipt().evaluation, evaluation_id: `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
    created_at: createdAt, comment: `Evaluation ${index}` }
}

/** HTTP parser が確認済みの一ページを表し、hook の page 間整合性だけを検証する。 */
function page(items: EvaluationRecord[], nextCursor: string | null = null): EvaluationPage {
  return { project_id: S.projectId, run_id: S.runId, result_id: S.resultId, items, next_cursor: nextCursor }
}

/** 対象切替は親 key による別 mount で行い、同じ所有者だけを再描画する。 */
function render(scope = S) {
  hookPhases.cursor = 0
  return useEvaluationHistory(scope.projectId, scope.runId, scope.resultId, expired, failed)
}

/** 共有 query と page 消費 effect を進め、deadline timer は進めない。 */
async function settle(scope = S) {
  for (let index = 0; index < 4; index++) { await hookMicrotasks(); render(scope); commitHooks() }
  return render(scope)
}

/** API に渡った cursor のみを取り出し、取得数を全件数と取り違えない。 */
function requestedCursors(): Array<string | null> { return vi.mocked(loadEvaluationPage).mock.calls.map((call) => call[3] ?? null) }

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('evaluation server cursor history', () => {
  it('requests the precise Result scope and treats a valid empty page as empty history', async () => {
    vi.mocked(loadEvaluationPage).mockResolvedValue(page([]))
    render(); commitHooks(); const current = await settle()
    expect(loadEvaluationPage).toHaveBeenCalledWith(S.projectId, S.runId, S.resultId, null, 20, expect.any(AbortSignal))
    expect(current).toMatchObject({ items: [], pending: false, failure: null, nextCursor: null })
    current.more(); await settle()
    expect(loadEvaluationPage).toHaveBeenCalledTimes(1)
  })

  it('loads one server page per explicit action and blocks same-tick duplicate pagination', async () => {
    const first = Array.from({ length: 20 }, (_, index) => record(index + 1))
    const second = Array.from({ length: 20 }, (_, index) => record(index + 21))
    vi.mocked(loadEvaluationPage).mockResolvedValueOnce(page(first, first.at(-1)!.evaluation_id))
      .mockResolvedValueOnce(page(second, second.at(-1)!.evaluation_id)).mockResolvedValueOnce(page([record(41)]))
    render(); commitHooks(); let current = await settle()
    expect(requestedCursors()).toEqual([null])
    current.more(); current.more(); current.refresh(); current = await settle()
    expect(current.items).toHaveLength(40)
    expect(requestedCursors()).toEqual([null, first.at(-1)!.evaluation_id])
    current.more(); current = await settle(); current.more(); await settle()
    expect(current.items.map((item) => item.evaluation_id)).toEqual([...first, ...second, record(41)].map((item) => item.evaluation_id))
    expect(current.nextCursor).toBeNull()
    expect(requestedCursors()).toEqual([null, first.at(-1)!.evaluation_id, second.at(-1)!.evaluation_id])
  })

  it('keeps a newly confirmed receipt when an older in-flight first page omits it', async () => {
    const response = deferred<EvaluationPage>()
    const first = Array.from({ length: 20 }, (_, index) => record(index + 1))
    const recent = record(99)
    vi.mocked(loadEvaluationPage).mockReturnValue(response.promise)
    render(); commitHooks(); await hookMicrotasks()
    render().remember(recent); response.resolve(page(first, first.at(-1)!.evaluation_id))
    const current = await settle()
    expect(current.items).toEqual([...first, recent])
    expect(current.nextCursor).toBe(first.at(-1)!.evaluation_id)
    expect(current.failure).toBeNull()
  })

  it('deduplicates the same receipt across confirmation, page loading and explicit refresh', async () => {
    const item = record(1)
    vi.mocked(loadEvaluationPage).mockResolvedValue(page([structuredClone(item)]))
    render(); commitHooks(); let current = await settle()
    current.remember(item); current.remember(structuredClone(item)); current = await settle()
    current.refresh(); current.refresh(); current = await settle()
    expect(current.items).toEqual([item])
    expect(current.failure).toBeNull()
    expect(requestedCursors()).toEqual([null, null])
  })

  it('sorts all records by exact microseconds and then UUID rather than arrival order', async () => {
    vi.mocked(loadEvaluationPage).mockResolvedValue(page([]))
    render(); commitHooks(); const current = await settle()
    const early = record(30, '2026-09-10T01:00:00.000001Z')
    const middle = record(10, '2026-09-10T01:00:00.000002Z')
    const late = record(20, '2026-09-10T01:00:00.000002Z')
    current.remember(late); current.remember(early); current.remember(middle)
    expect((await settle()).items).toEqual([early, middle, late])
  })

  it('refreshes from the server beginning without losing known immutable receipts or skipping unloaded rows', async () => {
    const first = Array.from({ length: 20 }, (_, index) => record(index + 1))
    const later = record(21)
    vi.mocked(loadEvaluationPage).mockResolvedValueOnce(page(first, first.at(-1)!.evaluation_id))
      .mockResolvedValueOnce(page([later])).mockResolvedValueOnce(page(first, first.at(-1)!.evaluation_id))
      .mockResolvedValueOnce(page([later, record(22)]))
    render(); commitHooks(); let current = await settle(); current.more(); current = await settle()
    current.remember(record(99)); current.refresh(); current = await settle()
    expect(current.items).toHaveLength(22)
    expect(current.nextCursor).toBe(first.at(-1)!.evaluation_id)
    current.more(); current = await settle()
    expect(requestedCursors()).toEqual([null, first.at(-1)!.evaluation_id, null, first.at(-1)!.evaluation_id])
    expect(current.items).toEqual([...first, later, record(22), record(99)])
  })

  it('does not start refresh or pagination while a read is still pending', async () => {
    const response = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValue(response.promise)
    render(); commitHooks(); let current = await settle()
    current.refresh(); current.more(); current = await settle()
    expect(loadEvaluationPage).toHaveBeenCalledTimes(1)
    expect(current.pending).toBe(true)
    response.resolve(page([])); await settle()
  })

  it('rejects a late page that contradicts a receipt confirmed while that page was loading', async () => {
    const response = deferred<EvaluationPage>()
    const item = record(1)
    vi.mocked(loadEvaluationPage).mockReturnValue(response.promise)
    render(); commitHooks(); await hookMicrotasks(); render().remember(item)
    response.resolve(page([{ ...item, comment: 'Changed immutable value' }]))
    const current = await settle()
    expect(current.items).toEqual([item])
    expect(current.failure).toEqual({ key: 'loadFailed' })
    expect(failed).toHaveBeenCalledWith({ key: 'loadFailed' })
  })

  it('rechecks receipt conflicts between loader completion and the page consumption effect', async () => {
    const response = deferred<EvaluationPage>()
    const item = record(1)
    vi.mocked(loadEvaluationPage).mockReturnValue(response.promise)
    render(); commitHooks(); await hookMicrotasks()
    response.resolve(page([{ ...item, comment: 'Stale conflicting page' }]))
    await hookMicrotasks()
    // loader の先行検証後でも、直近の保存を上書きしない。
    render().remember(item); commitHooks()
    const current = await settle()
    expect(current.items).toEqual([item])
    expect(current.failure).toEqual({ key: 'loadFailed' })
  })

  it('blocks pagination after a conflicting receipt and recovers only by explicit consistent refresh', async () => {
    const first = Array.from({ length: 20 }, (_, index) => record(index + 1))
    vi.mocked(loadEvaluationPage).mockResolvedValue(page(first, first.at(-1)!.evaluation_id))
    render(); commitHooks(); let current = await settle()
    current.remember({ ...first[0]!, rating: 5 }); current = await settle()
    expect(current.failure).toEqual({ key: 'loadFailed' })
    expect(current.items).toEqual(first)
    current.more(); await settle(); expect(loadEvaluationPage).toHaveBeenCalledTimes(1)
    current.refresh(); current = await settle()
    expect(current.failure).toBeNull()
    expect(current.items).toEqual(first)
    expect(requestedCursors()).toEqual([null, null])
  })

  it.each(['before', 'equal'] as const)('rejects a next-page row %s the original server cursor', async (position) => {
    const first = Array.from({ length: 20 }, (_, index) => record(index + 1))
    const wrong = position === 'equal' ? record(20) : record(19)
    vi.mocked(loadEvaluationPage).mockResolvedValueOnce(page(first, first.at(-1)!.evaluation_id)).mockResolvedValueOnce(page([wrong]))
    render(); commitHooks(); let current = await settle(); current.more(); current = await settle()
    expect(current.items).toEqual(first)
    expect(current.failure).toEqual({ key: 'loadFailed' })
    current.more(); await settle(); expect(loadEvaluationPage).toHaveBeenCalledTimes(2)
  })

  it('requires explicit refresh after an invalid server cursor and retries from the beginning', async () => {
    const first = Array.from({ length: 20 }, (_, index) => record(index + 1))
    vi.mocked(loadEvaluationPage).mockResolvedValueOnce(page(first, first.at(-1)!.evaluation_id))
      .mockRejectedValueOnce(new ApiProblemError('private', 400, 'invalid_evaluation_cursor')).mockResolvedValueOnce(page(first))
    render(); commitHooks(); let current = await settle(); current.more(); current = await settle()
    expect(current.failure).toEqual({ key: 'cursorInvalid' })
    current.more(); await settle(); expect(loadEvaluationPage).toHaveBeenCalledTimes(2)
    current.refresh(); current = await settle()
    expect(current.failure).toBeNull()
    expect(requestedCursors()).toEqual([null, first.at(-1)!.evaluation_id, null])
  })

  it.each([
    [401, undefined, 'sessionExpired'], [403, undefined, 'forbidden'],
    [404, 'project_not_found', 'notFound'], [409, 'result_not_available', 'resultUnavailable'],
    [503, undefined, 'unavailable'],
  ] as const)('propagates a current %s read refusal through the shared owner gate', async (status, code, key) => {
    vi.mocked(loadEvaluationPage).mockRejectedValue(new ApiProblemError('private', status, code))
    render(); commitHooks(); const current = await settle()
    expect(current.failure).toEqual({ key })
    expect(failed).toHaveBeenCalledTimes(1)
    expect(failed).toHaveBeenCalledWith({ key })
    expect(expired).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
    expect(current.items).toEqual([])
  })

  it.each(['success', '401'] as const)('discards late %s after the 30-second read deadline', async (outcome) => {
    const response = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValue(response.promise)
    render(); commitHooks(); await hookMicrotasks(); vi.advanceTimersByTime(30_000)
    if (outcome === 'success') response.resolve(page([record(1)])); else response.reject(new ApiProblemError('private', 401))
    const current = await settle()
    expect(current).toMatchObject({ items: [], pending: false, failure: { key: 'readTimeout' } })
    expect(vi.mocked(loadEvaluationPage).mock.calls[0]![5]?.aborted).toBe(true)
    expect(expired).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled()
  })

  it('checks the absolute deadline even when the timer has not fired', async () => {
    const response = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValue(response.promise)
    render(); commitHooks(); await hookMicrotasks(); vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401)); const current = await settle()
    expect(current.failure).toEqual({ key: 'readTimeout' })
    expect(expired).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled()
  })

  it.each(['success', '401'] as const)('does not leak old %s across a Result owner replacement', async (outcome) => {
    const response = deferred<EvaluationPage>()
    vi.mocked(loadEvaluationPage).mockReturnValueOnce(response.promise).mockResolvedValueOnce({ ...page([]), result_id: S.actorId })
    render(); commitHooks(); await hookMicrotasks(); render().remember(record(99))
    unmountHooks()
    const next = { ...S, resultId: S.actorId }
    render(next); commitHooks()
    if (outcome === 'success') response.resolve(page([record(1)])); else response.reject(new ApiProblemError('private', 401))
    const current = await settle(next)
    expect(current).toMatchObject({ items: [], nextCursor: null, failure: null })
    expect(vi.mocked(loadEvaluationPage).mock.calls[0]![5]?.aborted).toBe(true)
    expect(expired).not.toHaveBeenCalled(); expect(failed).not.toHaveBeenCalled()
  })
})
