import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError, changeScheduleStatus, loadProjectTasks, loadSchedule, loadScheduleActivity, loadSchedulePage, updateSchedule, type SchedulePage, type ScheduleRecord } from '../../src/api'
import { useScheduleRevision, type ScheduleChange } from '../../src/hooks/useScheduleRevision'
import { useSchedules } from '../../src/hooks/useSchedules'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'
import { scheduleActivityFixture } from '../fixtures/scheduleActivity'

/** DOM の代用ではなく、共有 hook の render/commit/transport の順序だけを操作する。 */
interface Slot { kind: string; value?: unknown; dependencies?: readonly unknown[]; cleanup?: void | (() => void) }
const phases = vi.hoisted(() => ({ cursor: 0, slots: [] as Slot[], layout: [] as Array<() => void>, passive: [] as Array<() => void> }))
vi.mock('react', () => {
  /** useState の初期化関数と同じ参照比較を維持し、再描画で門禁を初期化しない。 */
  const slot = (kind: string): Slot => {
    const index = phases.cursor++
    const previous = phases.slots[index]
    if (previous && previous.kind !== kind) throw new Error('Unexpected hook order')
    return previous ?? (phases.slots[index] = { kind })
  }
  const same = (left?: readonly unknown[], right?: readonly unknown[]): boolean => !!left && !!right
    && left.length === right.length && left.every((value, index) => Object.is(value, right[index]))
  /** passive 前の同期拒否を検査できるよう、effect は明示 commit まで実行しない。 */
  const effect = (queue: Array<() => void>, setup: () => void | (() => void), dependencies?: readonly unknown[]): void => {
    const state = slot('effect')
    if (!same(state.dependencies, dependencies)) queue.push(() => {
      state.cleanup?.(); state.dependencies = dependencies; state.cleanup = setup()
    })
  }
  return {
    useState: (initial: unknown) => {
      const state = slot('state')
      if (!Object.hasOwn(state, 'value')) state.value = typeof initial === 'function' ? initial() : initial
      return [state.value, (next: unknown) => { state.value = typeof next === 'function' ? next(state.value) : next }]
    },
    useRef: (initial: unknown) => { const state = slot('ref'); return state.value ?? (state.value = { current: initial }) },
    useMemo: (factory: () => unknown, dependencies: readonly unknown[]) => {
      const state = slot('memo')
      if (!same(state.dependencies, dependencies)) { state.value = factory(); state.dependencies = dependencies }
      return state.value
    },
    useCallback: (callback: unknown, dependencies: readonly unknown[]) => {
      const state = slot('callback')
      if (!same(state.dependencies, dependencies)) { state.value = callback; state.dependencies = dependencies }
      return state.value
    },
    useEffect: (setup: () => void | (() => void), dependencies?: readonly unknown[]) => effect(phases.passive, setup, dependencies),
    useLayoutEffect: (setup: () => void | (() => void), dependencies?: readonly unknown[]) => effect(phases.layout, setup, dependencies),
  }
})
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  loadSchedule: vi.fn(), loadScheduleActivity: vi.fn(), loadSchedulePage: vi.fn(), loadProjectTasks: vi.fn(),
  updateSchedule: vi.fn(), changeScheduleStatus: vi.fn() }))

const ORIGINAL = scheduleFixture()
const EDIT: ScheduleChange = { kind: 'edit', input: { name: 'Retained draft',
  definition: { kind: 'CRON', timezone: 'Asia/Tokyo', cron_expression: '0 4 * * *' },
  input: { objective: 'Retained input' }, sources: { issues: 'integration:original' } } }
const STATUS: ScheduleChange = { kind: 'status', status: 'PAUSED' }
let available = true
const current = (): boolean => available
const ended = vi.fn()

/** fixture の promise を手動で閉じ、abort を無視する transport を再現する。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
/** 同期 hook instance を再描画する。 */
function render(options: Partial<Parameters<typeof useScheduleRevision>[0]> = {}) {
  phases.cursor = 0
  return useScheduleRevision({ schedule: ORIGINAL, projectId: ORIGINAL.project_id,
    csrfToken: 'c'.repeat(32), isCurrent: current, onSessionEnded: ended, ...options })
}
/** 親の HTTP 拒否 ref と子 writer を接続し、再描画前の成功通知を検査する。 */
function renderManagerWriter() {
  phases.cursor = 0
  const manager = useSchedules(ORIGINAL.project_id, ended)
  const writer = useScheduleRevision({ schedule: ORIGINAL, projectId: ORIGINAL.project_id,
    csrfToken: 'c'.repeat(32), isCurrent: current, onSessionEnded: ended,
    disabled: !manager.canWrite, isWriteAllowed: manager.canEdit })
  return { manager, writer }
}
/** React の commit と promise queue だけを進める。時間は勝手に進めない。 */
async function flush(): Promise<void> {
  for (const action of phases.layout.splice(0)) action()
  for (const action of phases.passive.splice(0)) action()
  for (let index = 0; index < 16; index += 1) await Promise.resolve()
}
/** 原編集を結果不明または版衝突にする。自動 GET/再送は許可しない。 */
async function uncertain(status = 500) {
  vi.mocked(updateSchedule).mockRejectedValueOnce(new ApiProblemError('private', status))
  const hook = render(); await flush()
  expect(hook.submit(EDIT, vi.fn())).toBe(true)
  await flush()
  return render()
}
/** 一回の明示 GET を最後まで読み、手動採用は実行しない。 */
async function readCurrent(record = scheduleFixture({ row_version: 8 })) {
  vi.mocked(loadSchedule).mockResolvedValueOnce(record)
  render().reconcile()
  render(); await flush()
  return render()
}

beforeEach(() => {
  available = true
  phases.cursor = 0; phases.slots = []; phases.layout = []; phases.passive = []
  vi.clearAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
  vi.mocked(updateSchedule).mockResolvedValue(scheduleFixture({ row_version: 8 }))
  vi.mocked(changeScheduleStatus).mockResolvedValue(scheduleFixture({ row_version: 8, status: 'PAUSED' }))
  vi.mocked(loadScheduleActivity).mockResolvedValue(scheduleActivityFixture())
})
afterEach(() => {
  for (const slot of phases.slots) slot.cleanup?.()
  vi.useRealTimers(); vi.unstubAllGlobals()
})

describe('schedule revision boundaries', () => {
  it.each([EDIT, STATUS])('sends original version once for $kind despite same-tick calls', async (change) => {
    const hook = render(); await flush()
    const saved = vi.fn()
    expect(hook.submit(change, saved)).toBe(true)
    expect(hook.submit(change, saved)).toBe(false)
    await flush()
    expect(saved).toHaveBeenCalledOnce()
    if (change.kind === 'edit') expect(updateSchedule).toHaveBeenCalledWith(ORIGINAL.project_id,
      ORIGINAL.schedule_id, { ...change.input, expected_row_version: 7 }, 'c'.repeat(32), expect.any(AbortSignal))
    else expect(changeScheduleStatus).toHaveBeenCalledWith(ORIGINAL.project_id,
      ORIGINAL.schedule_id, 'PAUSED', 7, 'c'.repeat(32), expect.any(AbortSignal))
    expect(loadSchedule).not.toHaveBeenCalled()
  })

  it.each([409, 500])('requires explicit read and version adoption after HTTP %s', async (status) => {
    let hook = await uncertain(status)
    expect(hook.locked).toBe(true)
    expect(hook.intent?.payload).toMatchObject({ expected_row_version: 7, name: 'Retained draft' })
    expect(loadSchedule).not.toHaveBeenCalled()
    expect(hook.submit(EDIT, vi.fn())).toBe(false)
    hook = await readCurrent()
    expect(hook.canAdopt).toBe(true)
    expect(hook.submit(EDIT, vi.fn())).toBe(false)
    expect(hook.adopt()).toBe(true)
    expect(updateSchedule).toHaveBeenCalledTimes(1)
    hook = render()
    expect(hook.base?.row_version).toBe(8)
    expect(hook.previousUnknown !== null).toBe(status === 500)
    hook.submit(EDIT, vi.fn()); await flush()
    expect(vi.mocked(updateSchedule).mock.calls[1]?.[2]).toMatchObject({ name: 'Retained draft', expected_row_version: 8 })
  })

  it('invalidates old current facts synchronously before rereading', async () => {
    await uncertain(409)
    const hook = await readCurrent()
    expect(hook.canAdopt).toBe(true)
    hook.reconcile()
    expect(hook.adopt()).toBe(false)
    expect(updateSchedule).toHaveBeenCalledOnce()
  })

  it.each([401, 403, 404])('read HTTP %s closes both adoption and further reads before render', async (status) => {
    await uncertain()
    const hook = await readCurrent()
    vi.mocked(loadSchedule).mockRejectedValueOnce(new ApiProblemError('private', status))
    hook.reconcile(); render(); await flush()
    expect(hook.adopt()).toBe(false)
    const denied = render()
    expect(denied).toMatchObject({ accessDenied: true, facts: null, locked: true, phase: 'denied' })
    denied.reconcile(); render(); await flush()
    expect(loadSchedule).toHaveBeenCalledTimes(2)
    expect(denied.submit(EDIT, vi.fn())).toBe(false)
    expect(ended).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
  })

  it('a failed ordinary read holds unknown and permits only another explicit read', async () => {
    const hook = await uncertain()
    vi.mocked(loadSchedule).mockRejectedValueOnce(new Error('disconnect'))
    hook.reconcile(); render(); await flush()
    const failed = render()
    expect(failed.phase).toBe('unknown')
    expect(failed.readFailure?.key).toBe('readFailed')
    expect(failed.adopt()).toBe(false)
    expect(updateSchedule).toHaveBeenCalledOnce()
    expect((await readCurrent()).canAdopt).toBe(true)
  })

  it('does not adopt a response that changes the immutable exact task', async () => {
    await uncertain()
    const hook = await readCurrent(scheduleFixture({ row_version: 8, task_key: 'other' }))
    expect(hook.canAdopt).toBe(false)
    expect(hook.adopt()).toBe(false)
  })

  it('adopting a completed schedule preserves the draft intent but only allows a later archive', async () => {
    await uncertain(409)
    let hook = await readCurrent(scheduleFixture({ row_version: 8, status: 'COMPLETED' }))
    expect(hook.intent?.change).toEqual(EDIT)
    expect(hook.adopt()).toBe(true)
    hook = render()
    expect(hook.submit(EDIT, vi.fn())).toBe(false)
    vi.mocked(changeScheduleStatus).mockResolvedValueOnce(scheduleFixture({ row_version: 9, status: 'ARCHIVED' }))
    expect(hook.submit({ kind: 'status', status: 'ARCHIVED' }, vi.fn())).toBe(true)
    await flush()
    expect(updateSchedule).toHaveBeenCalledOnce()
    expect(vi.mocked(changeScheduleStatus).mock.calls[0]?.slice(2, 4)).toEqual(['ARCHIVED', 8])
  })

  it('does not adopt a live refreshed prop over an editing or unknown original', async () => {
    let hook = render(); await flush()
    hook = render({ schedule: scheduleFixture({ row_version: 9 }) })
    expect(hook.base?.row_version).toBe(7)
    await uncertain()
    hook = render({ schedule: scheduleFixture({ row_version: 10 }), syncLatestWhenIdle: true })
    expect(hook.base?.row_version).toBe(7)
    expect(hook.intent?.original.row_version).toBe(7)
  })

  it('follows a freshly read status version only while no intent exists', async () => {
    vi.mocked(changeScheduleStatus).mockResolvedValueOnce(scheduleFixture({ row_version: 9, status: 'PAUSED' }))
    render({ syncLatestWhenIdle: true }); await flush()
    const hook = render({ schedule: scheduleFixture({ row_version: 8 }), syncLatestWhenIdle: true })
    hook.submit(STATUS, vi.fn()); await flush()
    expect(vi.mocked(changeScheduleStatus).mock.calls[0]?.[3]).toBe(8)
  })

  it('does not revert a successful newer version to the parent old prop', async () => {
    const hook = render({ syncLatestWhenIdle: true }); await flush()
    hook.submit(STATUS, vi.fn()); await flush()
    expect(render({ syncLatestWhenIdle: true }).base?.row_version).toBe(8)
  })

  it.each([400, 422])('follows new read facts after a definitively rejected status HTTP %s without claiming success', async (status) => {
    vi.mocked(changeScheduleStatus).mockRejectedValueOnce(new ApiProblemError('private rejected', status))
    let hook = render({ syncLatestWhenIdle: true }); await flush()
    const saved = vi.fn()
    const failed = vi.fn()
    expect(hook.submit(STATUS, saved, failed)).toBe(true)
    await flush()
    hook = render({ syncLatestWhenIdle: true })
    expect(hook.phase).toBe('ready')
    expect(hook.pending).toBe(false)
    expect(hook.intent).toBeNull()
    expect(hook.failure?.key).toBe('rejected')
    expect(hook.base?.row_version).toBe(7)
    expect(saved).not.toHaveBeenCalled()
    expect(failed).toHaveBeenCalledWith({ key: 'rejected' })
    hook = render({ syncLatestWhenIdle: true, schedule: scheduleFixture({ row_version: 9, status: 'PAUSED' }) })
    expect(hook.base).toMatchObject({ row_version: 9, status: 'PAUSED' })
    expect(hook.failure?.key).toBe('rejected')
    expect(changeScheduleStatus).toHaveBeenCalledOnce()
    expect(loadSchedule).not.toHaveBeenCalled()
    vi.mocked(changeScheduleStatus).mockResolvedValueOnce(scheduleFixture({ row_version: 10, status: 'ARCHIVED' }))
    expect(hook.submit({ kind: 'status', status: 'ARCHIVED' }, saved)).toBe(true)
    await flush()
    expect(vi.mocked(changeScheduleStatus).mock.calls[1]?.slice(2, 4)).toEqual(['ARCHIVED', 9])
    expect(saved).toHaveBeenCalledOnce()
  })

  it.each([400, 422])('retains the original editing draft and base after rejected HTTP %s', async (status) => {
    await uncertain(status)
    const hook = render({ schedule: scheduleFixture({ row_version: 9 }) })
    expect(hook.phase).toBe('ready')
    expect(hook.failure?.key).toBe('rejected')
    expect(hook.intent?.change).toEqual(EDIT)
    expect(hook.intent?.payload).toMatchObject({ expected_row_version: 7, name: 'Retained draft' })
    expect(hook.base?.row_version).toBe(7)
    expect(updateSchedule).toHaveBeenCalledOnce()
    expect(loadSchedule).not.toHaveBeenCalled()
  })

  it.each([409, 500])('keeps the status intent and original base when HTTP %s is unresolved', async (status) => {
    vi.mocked(changeScheduleStatus).mockRejectedValueOnce(new ApiProblemError('private unresolved', status))
    let hook = render({ syncLatestWhenIdle: true }); await flush()
    const saved = vi.fn()
    hook.submit(STATUS, saved); await flush()
    hook = render({ syncLatestWhenIdle: true, schedule: scheduleFixture({ row_version: 9, status: 'PAUSED' }) })
    expect(hook.phase).toBe(status === 409 ? 'conflict' : 'unknown')
    expect(hook.pending).toBe(true)
    expect(hook.intent?.payload).toEqual({ status: 'PAUSED', expected_row_version: 7 })
    expect(hook.base).toMatchObject({ row_version: 7, status: 'ACTIVE' })
    expect(hook.submit(STATUS, saved)).toBe(false)
    expect(saved).not.toHaveBeenCalled()
    expect(changeScheduleStatus).toHaveBeenCalledOnce()
    expect(loadSchedule).not.toHaveBeenCalled()
  })

  it.each([401, 403, 404, 422])('classifies known write refusal HTTP %s without automatic retry', async (status) => {
    const hook = await uncertain(status)
    expect(hook.phase).toBe(status === 422 ? 'ready' : 'denied')
    expect(updateSchedule).toHaveBeenCalledOnce()
    expect(loadSchedule).not.toHaveBeenCalled()
    expect(ended).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
  })

  it('keeps original payload after a synthetic 30 second deadline and ignores a late success', async () => {
    const pending = deferred<ScheduleRecord>()
    vi.mocked(updateSchedule).mockReturnValueOnce(pending.promise)
    const hook = render(); await flush()
    const saved = vi.fn()
    hook.submit(EDIT, saved); await flush()
    vi.advanceTimersByTime(30_001); await flush()
    const unknown = render()
    expect(unknown.phase).toBe('unknown')
    expect(unknown.intent?.payload).toMatchObject({ expected_row_version: 7 })
    pending.resolve(scheduleFixture({ row_version: 8 })); await flush()
    expect(saved).not.toHaveBeenCalled()
    expect(render().phase).toBe('unknown')
  })

  it('disabling an in-flight writer keeps unknown instead of consuming old success', async () => {
    const pending = deferred<ScheduleRecord>()
    vi.mocked(updateSchedule).mockReturnValueOnce(pending.promise)
    const hook = render(); await flush()
    const saved = vi.fn()
    hook.submit(EDIT, saved); await flush()
    render({ disabled: true }); await flush()
    expect(render({ disabled: true }).phase).toBe('unknown')
    pending.resolve(scheduleFixture({ row_version: 8 })); await flush()
    expect(saved).not.toHaveBeenCalled()
  })

  it('an access refusal during sending retains the original request and ignores the old success', async () => {
    const pending = deferred<ScheduleRecord>()
    vi.mocked(updateSchedule).mockReturnValueOnce(pending.promise)
    const hook = render(); await flush()
    const saved = vi.fn()
    hook.submit(EDIT, saved); await flush()
    hook.denyAccess(true)
    pending.resolve(scheduleFixture({ row_version: 8 })); await flush()
    const denied = render()
    expect(denied.phase).toBe('denied')
    expect(denied.intent?.payload).toMatchObject({ expected_row_version: 7 })
    expect(saved).not.toHaveBeenCalled()
    expect(ended).toHaveBeenCalledOnce()
  })

  it('readonly keeps independent reconciliation available but cannot adopt or write', async () => {
    await uncertain()
    vi.mocked(loadSchedule).mockResolvedValueOnce(scheduleFixture({ row_version: 8 }))
    let hook = render({ disabled: true }); await flush()
    hook.reconcile(); render({ disabled: true }); await flush()
    hook = render({ disabled: true })
    expect(hook.facts?.row_version).toBe(8)
    expect(hook.adopt()).toBe(false)
    expect(hook.submit(EDIT, vi.fn())).toBe(false)
  })

  it('a changed owner before the queued transport prevents any write', async () => {
    const hook = render(); await flush()
    hook.submit(EDIT, vi.fn())
    available = false
    await flush()
    expect(updateSchedule).not.toHaveBeenCalled()
    expect(ended).not.toHaveBeenCalled()
  })

  it('a status operation owns its local gate without rejecting its own queued request', async () => {
    const locks = { editor: false, status: false }
    const hook = render({ isWriteAllowed: () => !locks.editor }); await flush()
    expect(hook.submit(STATUS, vi.fn())).toBe(true)
    locks.status = true
    expect(hook.submit(STATUS, vi.fn())).toBe(false)
    await flush()
    expect(changeScheduleStatus).toHaveBeenCalledOnce()
  })

  it.each(['before', 'queued'])('another writer blocks %s the status transport', async (when) => {
    let otherOwner = when === 'before'
    const hook = render({ isWriteAllowed: () => !otherOwner }); await flush()
    expect(hook.submit(STATUS, vi.fn())).toBe(when !== 'before')
    otherOwner = true
    await flush()
    expect(changeScheduleStatus).not.toHaveBeenCalled()
  })

  it.each([EDIT, STATUS])('retains unknown when synchronous eligibility disappears before $kind success', async (change) => {
    const pending = deferred<ScheduleRecord>()
    if (change.kind === 'edit') vi.mocked(updateSchedule).mockReturnValueOnce(pending.promise)
    else vi.mocked(changeScheduleStatus).mockReturnValueOnce(pending.promise)
    let eligible = true
    const options = { isWriteAllowed: () => eligible }
    const saved = vi.fn()
    const failed = vi.fn()
    const hook = render(options); await flush()
    expect(hook.submit(change, saved, failed)).toBe(true)
    await flush()
    eligible = false
    pending.resolve(scheduleFixture({ row_version: 8 })); await flush()
    const unknown = render(options)
    expect(saved).not.toHaveBeenCalled()
    expect(unknown.phase).toBe('unknown')
    expect(unknown.failure?.key).toBe('unknown')
    expect(unknown.intent?.change).toEqual(change)
    expect(unknown.intent?.original.row_version).toBe(7)
    expect(unknown.pending).toBe(true)
    expect(failed).toHaveBeenCalledWith({ key: 'unknown' })
    expect(ended).not.toHaveBeenCalled()
    eligible = true
    expect(unknown.submit(change, saved)).toBe(false)
    vi.mocked(loadSchedule).mockResolvedValueOnce(scheduleFixture({ row_version: 8 }))
    unknown.reconcile(); render(options); await flush()
    expect(render(options).adopt()).toBe(true)
  })

  it('does not adopt earlier facts after the parent eligibility ref closes before render', async () => {
    await uncertain()
    await readCurrent()
    let eligible = true
    const options = { isWriteAllowed: () => eligible }
    const hook = render(options)
    expect(hook.canAdopt).toBe(true)
    eligible = false
    expect(hook.adopt()).toBe(false)
    const blocked = render(options)
    expect(blocked.canAdopt).toBe(false)
    expect(blocked.phase).toBe('unknown')
    expect(blocked.intent?.payload).toMatchObject({ expected_row_version: 7 })
  })

  it('does not consume a status success after actual manager list HTTP 403 without an intervening render', async () => {
    const firstPage: SchedulePage = { schedules: [ORIGINAL], total: 1, limit: 25, offset: 0 }
    vi.mocked(loadSchedulePage).mockResolvedValue(firstPage)
    vi.mocked(loadProjectTasks).mockResolvedValue({ tasks: [documentTask()] })
    vi.mocked(loadSchedule).mockResolvedValue(ORIGINAL)
    let owner = renderManagerWriter()
    for (let index = 0; index < 4; index++) { await flush(); owner = renderManagerWriter() }
    owner.manager.select(ORIGINAL.schedule_id)
    owner = renderManagerWriter()
    for (let index = 0; index < 4; index++) { await flush(); owner = renderManagerWriter() }
    expect(owner.manager.canEdit()).toBe(true)
    const write = deferred<ScheduleRecord>()
    vi.mocked(changeScheduleStatus).mockReturnValueOnce(write.promise)
    const saved = vi.fn()
    expect(owner.writer.submit(STATUS, saved)).toBe(true)
    await flush()
    const list = deferred<SchedulePage>()
    vi.mocked(loadSchedulePage).mockReturnValueOnce(list.promise)
    owner.manager.list.refresh(); owner = renderManagerWriter(); await flush()
    list.reject(new ApiProblemError('private forbidden', 403)); await flush()
    expect(owner.manager.canEdit()).toBe(false)
    write.resolve(scheduleFixture({ row_version: 8, status: 'PAUSED' })); await flush()
    expect(saved).not.toHaveBeenCalled()
    owner = renderManagerWriter()
    expect(owner.writer.phase).toBe('unknown')
    expect(owner.writer.intent?.payload).toEqual({ status: 'PAUSED', expected_row_version: 7 })
    expect(owner.manager.readDenied?.key).toBe('accessUnavailable')
    expect(changeScheduleStatus).toHaveBeenCalledOnce()
    expect(loadSchedule).toHaveBeenCalledOnce()
    expect(ended).not.toHaveBeenCalled()
  })

  it('freezes the accepted body independently of caller draft mutation', async () => {
    const change = structuredClone(EDIT)
    const hook = render(); await flush()
    hook.submit(change, vi.fn())
    if (change.kind === 'edit') change.input.name = 'Unsent replacement'
    await flush()
    expect(vi.mocked(updateSchedule).mock.calls[0]?.[2].name).toBe('Retained draft')
  })

  it('a second unknown write records its newly adopted base, not the first conflict version', async () => {
    await uncertain(409)
    expect((await readCurrent()).adopt()).toBe(true)
    vi.mocked(updateSchedule).mockRejectedValueOnce(new Error('disconnected'))
    render().submit(EDIT, vi.fn()); await flush()
    const hook = render()
    expect(hook.phase).toBe('unknown')
    expect(hook.intent?.original.row_version).toBe(8)
    expect(hook.intent?.payload).toMatchObject({ expected_row_version: 8, name: 'Retained draft' })
  })

  it('does not notify the new session about an old read 401', async () => {
    const hook = await uncertain()
    const pending = deferred<ScheduleRecord>()
    vi.mocked(loadSchedule).mockReturnValueOnce(pending.promise)
    hook.reconcile(); render(); await flush()
    available = false
    pending.reject(new ApiProblemError('private', 401)); await flush()
    expect(ended).not.toHaveBeenCalled()
  })
})
