import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError, loadProjectTasks, loadSchedule, loadScheduleActivity, loadSchedulePage, type ScheduleActivity, type SchedulePage, type ScheduleRecord } from '../../src/api'
import { useSchedules } from '../../src/hooks/useSchedules'
import { DEMO_PROJECT } from '../fixtures'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'
import { scheduleActivityFixture } from '../fixtures/scheduleActivity'

/** React の commit と transport 完了の間を制御する。DOM/focus は別 browser 回帰で検証する。 */
interface Slot { value?: unknown; initialized?: boolean; deps?: readonly unknown[]; cleanup?: void | (() => void) }
const phases = vi.hoisted(() => ({ cursor: 0, slots: [] as Slot[], layout: [] as Array<() => void>, passive: [] as Array<() => void> }))
vi.mock('react', () => {
  /** 同じ owner の state/ref を再描画の間も保持する。 */
  const slot = (): Slot => phases.slots[phases.cursor++] ?? (phases.slots[phases.cursor - 1] = {})
  /** React と同じ依存の参照同一性を維持する。 */
  const same = (a?: readonly unknown[], b?: readonly unknown[]): boolean => Boolean(a && b && a.length === b.length && a.every((item, index) => Object.is(item, b[index])))
  /** Cleanup は render 時でなく指定された commit phase に実行する。 */
  const effect = (queue: Array<() => void>, setup: () => void | (() => void), deps?: readonly unknown[]): void => {
    const current = slot()
    if (same(current.deps, deps)) return
    queue.push(() => { current.cleanup?.(); current.deps = deps; current.cleanup = setup() })
  }
  /** 古い closure も保持し、同 tick の直接呼出しを検証する。 */
  const memo = (factory: () => unknown, deps?: readonly unknown[]): unknown => {
    const current = slot()
    if (!current.initialized || !same(current.deps, deps)) {
      current.value = factory(); current.deps = deps; current.initialized = true
    }
    return current.value
  }
  return {
    useState: (initial: unknown) => {
      const current = slot()
      if (!current.initialized) { current.value = initial; current.initialized = true }
      return [current.value, (value: unknown) => { current.value = typeof value === 'function' ? value(current.value) : value }]
    },
    useRef: (initial: unknown) => { const current = slot(); return current.value ?? (current.value = { current: initial }) },
    useCallback: (callback: unknown, deps?: readonly unknown[]) => memo(() => callback, deps), useMemo: memo,
    useLayoutEffect: (setup: () => void | (() => void), deps?: readonly unknown[]) => effect(phases.layout, setup, deps),
    useEffect: (setup: () => void | (() => void), deps?: readonly unknown[]) => effect(phases.passive, setup, deps),
  }
})
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  loadProjectTasks: vi.fn(), loadSchedule: vi.fn(), loadScheduleActivity: vi.fn(), loadSchedulePage: vi.fn() }))

/** Abort を無視するサーバー応答を任意の順序で完了する。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
/** Timer を進めず Promise の受理だけを処理する。 */
async function microtasks(): Promise<void> { for (let index = 0; index < 12; index++) await Promise.resolve() }
/** 指定された所有者内で production hook を再描画する。 */
function render(enabled = true) { phases.cursor = 0; return useSchedules(projectId, expired, enabled) }
/** 実装と同じ layout→passive 順序で request を開始/破棄する。 */
function commit(): void { for (const callback of phases.layout.splice(0)) callback(); for (const callback of phases.passive.splice(0)) callback() }
/** Effect の state 更新を再描画し、静的 mock return 値では query 境界を代用しない。 */
async function settle() {
  for (let index = 0; index < 4; index++) { render(); commit(); await microtasks() }
  return render()
}
/** 別 actor/context の mount では元の pending を移さない。 */
function unmount(): void {
  for (const current of phases.slots) current.cleanup?.()
  phases.cursor = 0; phases.slots = []; phases.layout = []; phases.passive = []
}
/** 件数・limit・offset を持つサーバーの頁。 */
function page(overrides: Partial<SchedulePage> = {}): SchedulePage { return { schedules: [scheduleFixture()], total: 101, limit: 25, offset: 0, ...overrides } }
const expired = vi.fn()
let projectId = DEMO_PROJECT.project_id
beforeEach(() => {
  vi.clearAllMocks(); projectId = DEMO_PROJECT.project_id
  vi.mocked(loadSchedulePage).mockResolvedValue(page())
  vi.mocked(loadProjectTasks).mockResolvedValue({ tasks: [documentTask()] })
  vi.mocked(loadSchedule).mockResolvedValue(scheduleFixture())
  vi.mocked(loadScheduleActivity).mockImplementation(async (project, id) => scheduleActivityFixture({ project_id: project, schedule_id: id }))
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
})
afterEach(() => { unmount(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('independent schedule queries', () => {
  it('does not read anything before Project authorization', async () => {
    render(false); commit(); await microtasks()
    expect(loadSchedulePage).not.toHaveBeenCalled(); expect(loadSchedule).not.toHaveBeenCalled(); expect(loadProjectTasks).not.toHaveBeenCalled()
    expect(loadScheduleActivity).not.toHaveBeenCalled()
  })
  it('retains the complete list metadata when the independent task catalog fails', async () => {
    vi.mocked(loadProjectTasks).mockRejectedValue(new Error('private catalog failure'))
    const current = await settle()
    expect(current.list.data?.total).toBe(101)
    expect(current.catalog.failure).toEqual({ key: 'loadFailed' })
    expect(current.readDenied).toBeNull()
  })
  it('loads original details only on selection, without editing a stale list row', async () => {
    let current = await settle()
    expect(loadSchedule).not.toHaveBeenCalled()
    current.select(scheduleFixture().schedule_id)
    expect(current.canEdit()).toBe(false)
    current = await settle()
    expect(loadSchedule).toHaveBeenCalledWith(projectId, scheduleFixture().schedule_id, expect.any(AbortSignal))
    expect(current.record?.row_version).toBe(7)
    expect(current.canWrite).toBe(true)
  })
  it('uses server filters and total rather than filtering the loaded first page', async () => {
    let current = await settle()
    current.search('literal_%', 'ARCHIVED'); current = await settle()
    expect(loadSchedulePage).toHaveBeenLastCalledWith(projectId, { q: 'literal_%', status: 'ARCHIVED', limit: 25, offset: 0 }, expect.any(AbortSignal))
    current.turnPage(100)
    vi.mocked(loadSchedulePage).mockResolvedValue(page({ offset: 100 }))
    current = await settle()
    expect(current.filter.offset).toBe(100)
    expect(current.list.data?.total).toBe(101)
  })
  it('moves to the previous valid page when a current server total shrinks', async () => {
    let current = await settle()
    vi.mocked(loadSchedulePage).mockImplementation(async (_project, options) => page({ schedules: [], total: 100, offset: options?.offset ?? 0 }))
    current.turnPage(100); current = await settle()
    expect(current.filter.offset).toBe(75)
    expect(loadSchedulePage).toHaveBeenLastCalledWith(projectId, expect.objectContaining({ offset: 75 }), expect.any(AbortSignal))
  })
  it.each([401, 200])('discards an old q=A response after q=A→B→A, including HTTP %s', async (status) => {
    const old = deferred<SchedulePage>()
    vi.mocked(loadSchedulePage).mockImplementationOnce(() => old.promise)
    let current = await settle()
    current.search('B', ''); current = await settle()
    current.search('', ''); current = await settle()
    if (status === 401) old.reject(new ApiProblemError('old private body', 401))
    else old.resolve(page({ total: 999 }))
    await microtasks(); current = render()
    expect(current.list.data?.total).toBe(101)
    expect(current.readDenied).toBeNull(); expect(expired).not.toHaveBeenCalled()
  })
  it('does not let an old actor response affect a new owner returning to the same Project', async () => {
    const old = deferred<SchedulePage>()
    vi.mocked(loadSchedulePage).mockImplementationOnce(() => old.promise)
    await settle(); unmount(); const current = await settle()
    old.reject(new ApiProblemError('old session', 401)); await microtasks()
    expect(current.list.data?.total).toBe(101); expect(expired).not.toHaveBeenCalled()
    expect(render().readDenied).toBeNull()
  })
  it('expires a query after 30 seconds and discards its later success', async () => {
    const old = deferred<SchedulePage>()
    vi.mocked(loadSchedulePage).mockImplementationOnce(() => old.promise)
    await settle(); await vi.advanceTimersByTimeAsync(30_000)
    expect(render().list.failure).toEqual({ key: 'loadFailed' })
    old.resolve(page()); await microtasks()
    expect(render().list.data).toBeNull()
  })
})

describe('independent pending occurrence reads', () => {
  it('starts on exact selection and refreshes independently without replacing detail or filter generations', async () => {
    let current = await settle()
    expect(loadScheduleActivity).not.toHaveBeenCalled()
    current.select(scheduleFixture().schedule_id); current = await settle()
    expect(loadScheduleActivity).toHaveBeenCalledWith(projectId, scheduleFixture().schedule_id, expect.any(AbortSignal))
    const detail = current.record
    current.activity.refresh(); current = await settle()
    expect(loadScheduleActivity).toHaveBeenCalledTimes(2)
    expect(loadSchedule).toHaveBeenCalledOnce()
    expect(current.record).toBe(detail)
    current.search('retained query', 'PAUSED'); current = await settle()
    current.refreshDetail(); current = await settle()
    expect(loadScheduleActivity).toHaveBeenCalledTimes(2)
    expect(current.canEdit()).toBe(true)
  })

  it.each(['server', 'network'])('keeps original history and write qualification on a generic activity %s failure', async (failure) => {
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    const previous = current.activity.data
    vi.mocked(loadScheduleActivity).mockRejectedValueOnce(failure === 'server'
      ? new ApiProblemError('private unavailable', 500) : new Error('private disconnected'))
    current.activity.refresh(); current = await settle()
    expect(current.activity.failure?.key).toBe('loadFailed')
    expect(current.activity.data).toBe(previous)
    expect(current.record?.schedule_id).toBe(scheduleFixture().schedule_id)
    expect(current.list.data?.total).toBe(101)
    expect(current.canEdit()).toBe(true)
    expect(current.readDenied).toBeNull()
  })

  it.each([401, 403, 404])('activity HTTP %s synchronously closes writing and is not cleared by activity success', async (status) => {
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    const allowed = current.canEdit
    const response = deferred<ScheduleActivity>()
    vi.mocked(loadScheduleActivity).mockReturnValueOnce(response.promise)
    current.activity.refresh(); current = await settle()
    response.reject(new ApiProblemError('private denied', status)); await microtasks()
    expect(allowed()).toBe(false)
    current = await settle()
    expect(current.readDenied?.key).toBe(status === 401 ? 'sessionExpired' : 'accessUnavailable')
    expect(current.record?.schedule_id).toBe(scheduleFixture().schedule_id)
    current.activity.refresh(); current = await settle()
    expect(current.activity.failure).toBeNull()
    expect(current.canEdit()).toBe(false)
    current.refreshDetail(); current = await settle()
    expect(current.canEdit()).toBe(status !== 401)
    expect(expired).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
  })

  it('does not let a late activity success or its manual refresh clear a list denial', async () => {
    const old = deferred<ScheduleActivity>()
    vi.mocked(loadScheduleActivity).mockReturnValueOnce(old.promise)
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    vi.mocked(loadSchedulePage).mockRejectedValueOnce(new ApiProblemError('denied', 403))
    current.list.refresh(); current = await settle()
    old.resolve(scheduleActivityFixture()); await microtasks(); current = await settle()
    expect(current.activity.failure).toBeNull()
    expect(current.readDenied?.key).toBe('accessUnavailable')
    current.activity.refresh(); current = await settle()
    expect(current.canEdit()).toBe(false)
    expect(current.readDenied?.key).toBe('accessUnavailable')
  })

  it.each([200, 401])('ignores activity HTTP %s from an earlier Schedule A→B→A owner', async (status) => {
    const old = deferred<ScheduleActivity>()
    vi.mocked(loadScheduleActivity).mockReturnValueOnce(old.promise)
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    current.select('abcdefab-0000-4000-8000-000000000099'); current = await settle()
    current.select(scheduleFixture().schedule_id); current = await settle()
    if (status === 401) old.reject(new ApiProblemError('old private session', 401))
    else old.resolve(scheduleActivityFixture({ row_version: 999 }))
    await microtasks(); current = await settle()
    expect(current.activity.data?.row_version).toBe(9)
    expect(current.activity.data?.schedule_id).toBe(scheduleFixture().schedule_id)
    expect(current.readDenied).toBeNull()
    expect(expired).not.toHaveBeenCalled()
  })

  it('closes the old activity transport synchronously when selection changes before render', async () => {
    const old = deferred<ScheduleActivity>()
    vi.mocked(loadScheduleActivity).mockReturnValueOnce(old.promise)
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    const signal = vi.mocked(loadScheduleActivity).mock.calls[0]![2]!
    current.select('abcdefab-0000-4000-8000-000000000099')
    current.select(scheduleFixture().schedule_id)
    expect(signal.aborted).toBe(true)
    old.reject(new ApiProblemError('old selection session', 401)); await microtasks()
    expect(signal.aborted).toBe(true)
    expect(expired).not.toHaveBeenCalled()
    current = await settle()
    expect(current.readDenied).toBeNull()
    expect(current.activity.data?.schedule_id).toBe(scheduleFixture().schedule_id)
    expect(loadScheduleActivity).toHaveBeenCalledTimes(2)
  })

  it.each(['project', 'actor-csrf'])('ignores an old activity 401 after %s owner replacement and return', async (context) => {
    const old = deferred<ScheduleActivity>()
    vi.mocked(loadScheduleActivity).mockReturnValueOnce(old.promise)
    let current = await settle(); current.select(scheduleFixture().schedule_id); await settle()
    unmount()
    if (context === 'project') projectId = 'abcdefab-0000-4000-8000-000000000090'
    current = await settle(); current.select(scheduleFixture().schedule_id); await settle()
    unmount(); projectId = DEMO_PROJECT.project_id
    current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    old.reject(new ApiProblemError('old session', 401)); await microtasks(); current = await settle()
    expect(current.activity.data?.project_id).toBe(DEMO_PROJECT.project_id)
    expect(current.readDenied).toBeNull()
    expect(expired).not.toHaveBeenCalled()
  })

  it('keeps activity unavailable after a synthetic deadline and discards a late pending response', async () => {
    const old = deferred<ScheduleActivity>()
    vi.mocked(loadScheduleActivity).mockReturnValueOnce(old.promise)
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    await vi.advanceTimersByTimeAsync(30_000); current = await settle()
    expect(current.activity.failure?.key).toBe('loadFailed')
    expect(current.activity.data).toBeNull()
    expect(current.record?.schedule_id).toBe(scheduleFixture().schedule_id)
    old.resolve(scheduleActivityFixture()); await microtasks(); current = await settle()
    expect(current.activity.data).toBeNull()
    expect(current.activity.failure?.key).toBe('loadFailed')
    expect(current.canEdit()).toBe(true)
  })
})

describe('shared Project read-denial boundary', () => {
  it.each([401, 403, 404])('closes all write eligibility synchronously on list HTTP %s and ignores late detail success', async (status) => {
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    const originalCanEdit = current.canEdit
    const listRead = deferred<SchedulePage>(); const detailRead = deferred<ScheduleRecord>()
    vi.mocked(loadSchedulePage).mockImplementationOnce(() => listRead.promise)
    vi.mocked(loadSchedule).mockImplementationOnce(() => detailRead.promise)
    current.refreshDetail(); current.list.refresh(); await settle()
    listRead.reject(new ApiProblemError('private denied', status)); await microtasks()
    expect(originalCanEdit()).toBe(false)
    detailRead.resolve(scheduleFixture()); await microtasks(); current = await settle()
    expect(current.canWrite).toBe(false)
    expect(current.readDenied?.key).toBe(status === 401 ? 'sessionExpired' : 'accessUnavailable')
    current.refreshDetail(); current = await settle()
    expect(current.canWrite).toBe(status !== 401)
    expect(expired).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
  })
  it('keeps an active denial while an automatic original detail read completes', async () => {
    const original = deferred<ScheduleRecord>()
    vi.mocked(loadSchedule).mockImplementationOnce(() => original.promise)
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    vi.mocked(loadSchedulePage).mockRejectedValueOnce(new ApiProblemError('denied', 403))
    current.list.refresh(); current = await settle()
    original.resolve(scheduleFixture()); await microtasks(); current = await settle()
    expect(current.readDenied).toEqual({ key: 'accessUnavailable' }); expect(current.canEdit()).toBe(false)
  })
  it('does not grant access from an automatic post-save read or reuse an interrupted manual proof', async () => {
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    vi.mocked(loadSchedulePage).mockRejectedValueOnce(new ApiProblemError('denied', 403))
    current.list.refresh(); current = await settle()
    current.refreshFacts(); current = await settle()
    expect(current.readDenied).toEqual({ key: 'accessUnavailable' }); expect(current.canEdit()).toBe(false)
    const manual = deferred<ScheduleRecord>()
    vi.mocked(loadSchedule).mockImplementationOnce(() => manual.promise)
    current.refreshDetail(); current = await settle()
    current.refreshFacts(); current = await settle()
    manual.resolve(scheduleFixture()); await microtasks(); current = await settle()
    expect(current.readDenied).toEqual({ key: 'accessUnavailable' }); expect(current.canEdit()).toBe(false)
    current.refreshDetail(); current = await settle()
    expect(current.readDenied).toBeNull(); expect(current.canEdit()).toBe(true)
  })
  it.each(['catalog', 'detail'] as const)('propagates %s access denial without deleting original detail facts', async (source) => {
    let current = await settle(); current.select(scheduleFixture().schedule_id); current = await settle()
    if (source === 'catalog') {
      vi.mocked(loadProjectTasks).mockRejectedValueOnce(new ApiProblemError('denied', 403)); current.refreshCatalog()
    } else {
      vi.mocked(loadSchedule).mockRejectedValueOnce(new ApiProblemError('denied', 404)); current.refreshDetail()
    }
    current = await settle()
    expect(current.record?.schedule_id).toBe(scheduleFixture().schedule_id)
    expect(current.readDenied).toEqual({ key: 'accessUnavailable' }); expect(current.canWrite).toBe(false)
  })
})
