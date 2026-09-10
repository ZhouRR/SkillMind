import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadTaskFlowPreview } from '../../src/api'
import { useTaskFlowPreview } from '../../src/hooks/useTaskFlowPreview'
import { FLOW_ACTOR, FLOW_PROJECT, FLOW_TARGET, flowPreview } from '../fixtures/taskFlow'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), loadTaskFlowPreview: vi.fn() }))
let actor = FLOW_ACTOR
let project = FLOW_PROJECT
let session = 'synthetic-session'
const ended = vi.fn()
/** shared query を実際に呼び、owner 変更の前後を同じ hook runtime で制御する。 */
function render() {
  hookPhases.cursor = 0
  return useTaskFlowPreview({ actorId: actor, projectId: project, sessionKey: session, onSessionEnded: ended })
}
/** 実 timer は進めず、現在の読取 Promise と commit だけを収束させる。 */
async function settle() {
  for (let count = 0; count < 6; count++) { await hookMicrotasks(); render(); commitHooks() }
  return render()
}
beforeEach(() => {
  vi.resetAllMocks(); actor = FLOW_ACTOR; project = FLOW_PROJECT; session = 'synthetic-session'
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout })
  vi.mocked(loadTaskFlowPreview).mockResolvedValue(flowPreview())
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })
describe('read-only task flow ownership', () => {
  it('does not fetch until explicit selection and freezes only target identity', async () => {
    const initial = render(); commitHooks(); await settle()
    expect(loadTaskFlowPreview).not.toHaveBeenCalled()
    const original = { ...FLOW_TARGET }
    initial.select(original); original.task_key = 'changed'
    const current = await settle()
    expect(loadTaskFlowPreview).toHaveBeenCalledTimes(1)
    expect(loadTaskFlowPreview).toHaveBeenCalledWith(FLOW_PROJECT, FLOW_TARGET, expect.any(AbortSignal))
    expect(current.data).toEqual(flowPreview())
  })
  it('dispatches only final selection for same-tick A/B/A actions', async () => {
    const current = render(); commitHooks()
    current.select(FLOW_TARGET); current.select({ ...FLOW_TARGET, task_key: 'other' }); current.select(FLOW_TARGET)
    await settle(); expect(loadTaskFlowPreview).toHaveBeenCalledTimes(1)
    expect(vi.mocked(loadTaskFlowPreview).mock.calls[0]?.[1].task_key).toBe('review')
  })
  it.each([200, 401])('closing synchronously discards late %s even before render', async (status) => {
    const gate = deferred<ReturnType<typeof flowPreview>>()
    vi.mocked(loadTaskFlowPreview).mockReturnValueOnce(gate.promise)
    const initial = render(); commitHooks(); initial.select(FLOW_TARGET)
    const current = await settle(); expect(current.pending).toBe(true)
    current.close()
    if (status === 200) gate.resolve(flowPreview()); else gate.reject(new ApiProblemError('private', 401))
    await hookMicrotasks(); expect(ended).not.toHaveBeenCalled()
    const closed = await settle(); expect(closed.data).toBeNull(); expect(closed.target).toBeNull()
  })
  it.each(['actor', 'project', 'session'] as const)('discards late 401 and retained callbacks after %s change', async (dimension) => {
    const gate = deferred<ReturnType<typeof flowPreview>>()
    vi.mocked(loadTaskFlowPreview).mockReturnValueOnce(gate.promise)
    let current = render(); commitHooks(); current.select(FLOW_TARGET); current = await settle()
    if (dimension === 'actor') actor = FLOW_TARGET.skill_id
    if (dimension === 'project') project = FLOW_TARGET.skill_id
    if (dimension === 'session') session = 'new-session'
    render()
    expect(current.select(FLOW_TARGET)).toBe(false)
    gate.reject(new ApiProblemError('private', 401))
    const changed = await settle()
    expect(changed.target).toBeNull(); expect(ended).not.toHaveBeenCalled()
  })
  it('notifies current session expiry once without displaying server detail', async () => {
    vi.mocked(loadTaskFlowPreview).mockRejectedValue(new ApiProblemError('private', 401))
    const initial = render(); commitHooks(); initial.select(FLOW_TARGET)
    const current = await settle()
    expect(current.failure).toEqual({ key: 'sessionExpired' }); expect(ended).toHaveBeenCalledTimes(1)
  })
  it.each([[404, 'task_flow_preview_not_found', 'unavailable'], [409, 'task_flow_preview_invalid', 'invalid'], [503, 'task_flow_preview_unavailable', 'loadFailed']] as const)('preserves controlled %s failure instead of an empty plan', async (status, code, key) => {
    vi.mocked(loadTaskFlowPreview).mockRejectedValue(new ApiProblemError('private', status, code))
    const initial = render(); commitHooks(); initial.select(FLOW_TARGET)
    const current = await settle(); expect(current.failure).toEqual({ key }); expect(current.data).toBeNull()
  })
  it('times out at 30s and rejects a transport that ignores abort', async () => {
    const gate = deferred<ReturnType<typeof flowPreview>>()
    vi.mocked(loadTaskFlowPreview).mockReturnValueOnce(gate.promise)
    const initial = render(); commitHooks(); initial.select(FLOW_TARGET); await settle()
    await vi.advanceTimersByTimeAsync(30000)
    expect((await settle()).failure).toEqual({ key: 'timeout' })
    gate.reject(new ApiProblemError('private', 401)); await settle(); expect(ended).not.toHaveBeenCalled()
  })
  it('checks absolute deadline before late expiry side effects when timer has not fired', async () => {
    const gate = deferred<ReturnType<typeof flowPreview>>()
    vi.mocked(loadTaskFlowPreview).mockReturnValueOnce(gate.promise)
    const initial = render(); commitHooks(); initial.select(FLOW_TARGET); await settle()
    vi.spyOn(performance, 'now').mockReturnValue(31000)
    gate.reject(new ApiProblemError('private', 401))
    expect((await settle()).failure).toEqual({ key: 'timeout' }); expect(ended).not.toHaveBeenCalled()
  })
  it('manual refresh hides old plan while pending and on failure', async () => {
    const initial = render(); commitHooks(); initial.select(FLOW_TARGET)
    const current = await settle(); expect(current.data).not.toBeNull()
    const gate = deferred<ReturnType<typeof flowPreview>>()
    vi.mocked(loadTaskFlowPreview).mockReturnValueOnce(gate.promise)
    current.refresh(); expect((await settle()).data).toBeNull()
    gate.reject(new ApiProblemError('private', 503)); expect((await settle()).data).toBeNull()
  })
  it('blocks invalid catalog identities and unmounted callbacks without any fetch', async () => {
    const initial = render(); commitHooks()
    expect(initial.select({ ...FLOW_TARGET, task_id: 'invalid' })).toBe(false)
    unmountHooks(); expect(initial.select(FLOW_TARGET)).toBe(false)
    await hookMicrotasks(); expect(loadTaskFlowPreview).not.toHaveBeenCalled()
  })
})
