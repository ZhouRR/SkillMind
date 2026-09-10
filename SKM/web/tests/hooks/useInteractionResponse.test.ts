import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError, respondToInteraction } from '../../src/api'
import { useInteractionResponse } from '../../src/hooks/useInteractionResponse'
import { INTERACTION_SCOPE, interactionFixture, interactionReceipt } from '../fixtures/interaction'

/** DOM なしで同 tick と layout/passive の間を制御する。実 DOM は browser suite が担当する。 */
interface Slot {
  value?: unknown
  initialized?: boolean
  deps?: readonly unknown[]
  cleanup?: void | (() => void)
}
const phases = vi.hoisted(() => ({ cursor: 0, slots: [] as Slot[], layout: [] as Array<() => void>, passive: [] as Array<() => void> }))

vi.mock('react', () => {
  /** 一回の owner 内では同じ state/ref を返す。 */
  const slot = (): Slot => phases.slots[phases.cursor++] ?? (phases.slots[phases.cursor - 1] = {})
  /** 依存の参照同一性は実 React と同じ規則にする。 */
  const same = (a?: readonly unknown[], b?: readonly unknown[]): boolean => Boolean(a && b && a.length === b.length && a.every((item, index) => Object.is(item, b[index])))
  /** callback の呼出し時点と commit 時点を混同しない。 */
  const effect = (queue: Array<() => void>, setup: () => void | (() => void), deps?: readonly unknown[]): void => {
    const current = slot()
    if (same(current.deps, deps)) return
    queue.push(() => { current.cleanup?.(); current.deps = deps; current.cleanup = setup() })
  }
  /** memoized callback を元 request の closure として保持する。 */
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
    useCallback: (callback: unknown, deps?: readonly unknown[]) => memo(() => callback, deps),
    useMemo: memo,
    useLayoutEffect: (setup: () => void | (() => void), deps?: readonly unknown[]) => effect(phases.layout, setup, deps),
    useEffect: (setup: () => void | (() => void), deps?: readonly unknown[]) => effect(phases.passive, setup, deps),
  }
})
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), respondToInteraction: vi.fn() }))

/** Abort を無視する transport を手動完了させる。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

/** Timer を進めず Promise の連鎖だけを排出する。 */
async function microtasks(): Promise<void> { for (let index = 0; index < 8; index++) await Promise.resolve() }

/** 本物の Hook 関数を同じ owner として再描画する。 */
function render(overrides: Partial<Parameters<typeof useInteractionResponse>[0]> = {}) {
  phases.cursor = 0
  return useInteractionResponse({ scope: INTERACTION_SCOPE, interaction: question, csrfToken: 'synthetic-session',
    available: true, writable: true, onResponded: confirmed, onSessionExpired: expired, ...overrides })
}

/** Commit を明示し、同 tick の呼出しでは勝手に state を再描画しない。 */
function commit(): void {
  for (const callback of phases.layout.splice(0)) callback()
  for (const callback of phases.passive.splice(0)) callback()
}

/** 一つの key の所有権を破棄する。次の mount は別の ref/state を持つ。 */
function unmount(): void {
  for (const current of phases.slots) current.cleanup?.()
  phases.cursor = 0; phases.slots = []; phases.layout = []; phases.passive = []
}

const confirmed = vi.fn()
const expired = vi.fn()
let question = interactionFixture()
beforeEach(() => {
  vi.clearAllMocks()
  question = interactionFixture()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
})
afterEach(() => { unmount(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('original interaction request ownership', () => {
  it('rejects same-tick double submission before React rerenders', async () => {
    const response = deferred<ReturnType<typeof interactionReceipt>>()
    vi.mocked(respondToInteraction).mockReturnValue(response.promise)
    const hook = render(); commit()
    hook.start({ text: 'original' })
    hook.start({ text: 'second' })
    await microtasks()
    expect(respondToInteraction).toHaveBeenCalledTimes(1)
    expect(respondToInteraction).toHaveBeenCalledWith(INTERACTION_SCOPE.projectId, INTERACTION_SCOPE.runId,
      question.interaction_id, 3, { text: 'original' }, expect.any(String), 'synthetic-session', expect.any(AbortSignal))
    response.resolve(interactionReceipt()); await microtasks()
    expect(render().pending?.phase).toBe('confirmed')
    expect(confirmed).toHaveBeenCalledExactlyOnceWith(interactionReceipt())
  })

  it('keeps the original version body and key through unknown and explicit confirmation', async () => {
    vi.mocked(respondToInteraction).mockRejectedValueOnce(new TypeError('private transport detail'))
      .mockResolvedValueOnce({ ...interactionReceipt(), idempotent_replay: true, status: 'RUNNING', row_version: 10 })
    const hook = render(); commit()
    const answer = { text: 'original', selected_option_keys: ['b', 'a'] }
    hook.start(answer); answer.text = 'edited'; answer.selected_option_keys.reverse()
    await microtasks()
    const first = vi.mocked(respondToInteraction).mock.calls[0]!
    question = { ...question, status: 'RESPONDED', version: 4 }
    const next = render(); commit()
    expect(next.pending?.phase).toBe('unknown')
    next.start({ text: 'must not replace original' })
    next.confirmOriginal(); next.confirmOriginal()
    await microtasks()
    const second = vi.mocked(respondToInteraction).mock.calls[1]!
    expect(second.slice(0, 7)).toEqual(first.slice(0, 7))
    expect(render().pending?.receipt).toMatchObject({ status: 'RUNNING', row_version: 10 })
    expect(respondToInteraction).toHaveBeenCalledTimes(2)
  })

  it('makes timeout unknown and ignores a successful response arriving afterwards', async () => {
    const response = deferred<ReturnType<typeof interactionReceipt>>()
    vi.mocked(respondToInteraction).mockReturnValue(response.promise)
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    vi.advanceTimersByTime(30_000)
    expect(render().pending).toMatchObject({ phase: 'unknown', uncertain: true })
    expect(vi.mocked(respondToInteraction).mock.calls[0]![7]?.aborted).toBe(true)
    response.resolve(interactionReceipt()); await microtasks()
    expect(confirmed).not.toHaveBeenCalled()
    expect(render().pending?.phase).toBe('unknown')
  })

  it('allows same-Run loading without interrupting the original in-flight request', async () => {
    const response = deferred<ReturnType<typeof interactionReceipt>>()
    vi.mocked(respondToInteraction).mockReturnValue(response.promise)
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    const loading = render({ writable: false }); commit()
    expect(loading.pending?.phase).toBe('sending')
    expect(vi.mocked(respondToInteraction).mock.calls[0]![7]?.aborted).toBe(false)
    response.resolve(interactionReceipt()); await microtasks()
    expect(confirmed).toHaveBeenCalledTimes(1)
  })

  it('blocks synchronous confirmation once the original interaction is absent', async () => {
    vi.mocked(respondToInteraction).mockRejectedValue(new Error('network'))
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    const absent = render({ available: false })
    absent.confirmOriginal()
    await microtasks()
    expect(respondToInteraction).toHaveBeenCalledTimes(1)
  })

  it('interrupts an absent target and never resurrects its late response when it returns', async () => {
    const response = deferred<ReturnType<typeof interactionReceipt>>()
    vi.mocked(respondToInteraction).mockReturnValue(response.promise)
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    render({ available: false }); commit()
    expect(render({ available: true }).pending?.phase).toBe('unknown'); commit()
    response.resolve(interactionReceipt()); await microtasks()
    expect(confirmed).not.toHaveBeenCalled()
  })

  it.each(['success', '401'] as const)('rejects late %s from the first A after A-B-A remount', async (outcome) => {
    const response = deferred<ReturnType<typeof interactionReceipt>>()
    vi.mocked(respondToInteraction).mockReturnValue(response.promise)
    const hook = render(); commit(); hook.start({ text: 'original A' }); await microtasks()
    unmount(); render({ scope: { ...INTERACTION_SCOPE, actorId: 'different actor' } }); commit()
    unmount(); render(); commit()
    if (outcome === '401') response.reject(new ApiProblemError('private', 401))
    else response.resolve(interactionReceipt())
    await microtasks()
    expect(confirmed).not.toHaveBeenCalled(); expect(expired).not.toHaveBeenCalled()
    expect(render().pending).toBeNull()
  })

  it('does not send an operation queued immediately before layout unmount', async () => {
    const hook = render(); commit(); hook.start({ text: 'original' }); unmount()
    await microtasks()
    expect(respondToInteraction).not.toHaveBeenCalled()
  })

  it('notifies current-session expiry once without retaining a sending phase', async () => {
    vi.mocked(respondToInteraction).mockRejectedValue(new ApiProblemError('private', 401))
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    expect(expired).toHaveBeenCalledTimes(1)
    const rejected = render()
    expect(rejected.pending).toMatchObject({ phase: 'rejected', failure: { key: 'sessionExpired' } })
    rejected.confirmOriginal(); rejected.edit(); rejected.start({ text: 'other' }); await microtasks()
    expect(respondToInteraction).toHaveBeenCalledTimes(1)
  })

  it('permits editing after an initial known 422 but never after an earlier unknown', async () => {
    const invalid = new ApiProblemError('private', 422, 'interaction_response_invalid')
    vi.mocked(respondToInteraction).mockRejectedValueOnce(invalid)
      .mockRejectedValueOnce(new TypeError('network')).mockRejectedValueOnce(invalid)
    const hook = render(); commit(); hook.start({ text: 'invalid' }); await microtasks()
    render().edit(); expect(render().pending).toBeNull()
    render().start({ text: 'unknown' }); await microtasks()
    render().confirmOriginal(); await microtasks()
    render().edit()
    expect(render().pending).toMatchObject({ phase: 'rejected', uncertain: true })
  })

  it.each(['forbidden', 'notFound'] as const)('closes POST after GET %s without clearing unknown identity', async (key) => {
    vi.mocked(respondToInteraction).mockRejectedValue(new TypeError('network'))
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    const request = render().pending?.request
    render().rejectAccess({ key }); render().confirmOriginal(); await microtasks()
    expect(render().pending).toMatchObject({ request, uncertain: true, phase: 'rejected', failure: { key } })
    expect(respondToInteraction).toHaveBeenCalledTimes(1)
  })

  it('closes the original POST gate on a read 401 even when the callback does not unmount', async () => {
    vi.mocked(respondToInteraction).mockRejectedValue(new TypeError('network'))
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    const request = render().pending?.request
    render().onReadSessionExpired(); render().onReadSessionExpired()
    render().confirmOriginal(); render().edit(); await microtasks()
    expect(expired).toHaveBeenCalledTimes(1)
    expect(render().pending).toMatchObject({ request, uncertain: true, phase: 'rejected', failure: { key: 'sessionExpired' } })
    expect(respondToInteraction).toHaveBeenCalledTimes(1)
  })

  it.each(['sessionExpired', 'csrfRejected', 'projectArchived', 'forbidden', 'notFound'] as const)(
    'blocks same-tick original confirmation after Workspace detail reports %s', async (key) => {
      vi.mocked(respondToInteraction).mockRejectedValue(new TypeError('network'))
      const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
      const request = render().pending?.request
      const denied = render({ accessFailure: { key } })
      denied.confirmOriginal(); await microtasks()
      expect(respondToInteraction).toHaveBeenCalledTimes(1)
      commit()
      expect(render().pending).toMatchObject({ request, uncertain: true, phase: 'rejected', failure: { key } })
      render().confirmOriginal(); render().edit(); await microtasks()
      expect(respondToInteraction).toHaveBeenCalledTimes(1)
      expect(expired).not.toHaveBeenCalled()
    },
  )

  it.each(['success', '401'] as const)('interrupts a pending write on Workspace access refusal and ignores late %s', async (outcome) => {
    const response = deferred<ReturnType<typeof interactionReceipt>>()
    vi.mocked(respondToInteraction).mockReturnValue(response.promise)
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    const request = render().pending?.request
    render({ accessFailure: { key: 'forbidden' } }); commit()
    expect(vi.mocked(respondToInteraction).mock.calls[0]![7]?.aborted).toBe(true)
    if (outcome === '401') response.reject(new ApiProblemError('private', 401))
    else response.resolve(interactionReceipt())
    await microtasks()
    expect(render().pending).toMatchObject({ request, uncertain: true, phase: 'rejected', failure: { key: 'forbidden' } })
    expect(confirmed).not.toHaveBeenCalled(); expect(expired).not.toHaveBeenCalled()
  })

  it('does not reopen a refused owner when no original answer existed yet', async () => {
    render(); commit()
    const denied = render({ accessFailure: { key: 'notFound' } })
    denied.start({ text: 'must not send' }); await microtasks()
    expect(respondToInteraction).not.toHaveBeenCalled()
    commit()
    render().start({ text: 'a later refresh cannot remove refusal' }); await microtasks()
    expect(respondToInteraction).not.toHaveBeenCalled()
  })

  it('preserves unknown identity but blocks confirmation after the actual project_archived 409', async () => {
    vi.mocked(respondToInteraction).mockRejectedValueOnce(new TypeError('network'))
      .mockRejectedValueOnce(new ApiProblemError('private', 409, 'project_archived'))
    const hook = render(); commit(); hook.start({ text: 'original' }); await microtasks()
    const request = render().pending?.request
    render().confirmOriginal(); await microtasks()
    expect(render().pending).toMatchObject({ request, uncertain: true, phase: 'rejected', failure: { key: 'projectArchived' } })
    render().confirmOriginal(); render().edit(); render().start({ text: 'new' }); await microtasks()
    expect(respondToInteraction).toHaveBeenCalledTimes(2)
  })

  it('recognizes normalized UUID spelling without rewriting the original request', async () => {
    const lower = { actorId: INTERACTION_SCOPE.actorId.replace(/.$/, 'a'),
      projectId: INTERACTION_SCOPE.projectId.replace(/.$/, 'b'), runId: INTERACTION_SCOPE.runId.replace(/.$/, 'c') }
    const upper = { actorId: lower.actorId.toUpperCase(), projectId: lower.projectId.toUpperCase(), runId: lower.runId.toUpperCase() }
    question = { ...question, interaction_id: 'abcdefab-abcd-4abc-8abc-abcdefabcdef'.toUpperCase() }
    vi.mocked(respondToInteraction).mockRejectedValueOnce(new TypeError('network')).mockResolvedValueOnce(interactionReceipt())
    const hook = render({ scope: upper }); commit(); hook.start({ text: 'original' }); await microtasks()
    const first = vi.mocked(respondToInteraction).mock.calls[0]!
    question = { ...question, interaction_id: question.interaction_id.toLowerCase() }
    render({ scope: lower }).confirmOriginal(); await microtasks()
    expect(respondToInteraction).toHaveBeenCalledTimes(2)
    expect(vi.mocked(respondToInteraction).mock.calls[1]!.slice(0, 7)).toEqual(first.slice(0, 7))
  })

  it('rejects a direct new answer for historical choice options with duplicate keys', async () => {
    question = interactionFixture('CHOICE')
    question = { ...question, options: [...question.options, { key: 'a', label: 'Historical duplicate' }] }
    vi.mocked(respondToInteraction).mockReturnValue(deferred<ReturnType<typeof interactionReceipt>>().promise)
    const hook = render(); commit()
    hook.start({ selected_option_keys: ['a'], text: 'must not submit' }); await microtasks()
    expect(respondToInteraction).not.toHaveBeenCalled()
    expect(render().pending).toBeNull()
  })

  it('checks current duplicate choice keys before a stale start callback can create an answer', async () => {
    question = interactionFixture('CHOICE')
    vi.mocked(respondToInteraction).mockReturnValue(deferred<ReturnType<typeof interactionReceipt>>().promise)
    const hook = render(); commit()
    const oldStart = hook.start
    question = { ...question, options: [...question.options, { key: 'a', label: 'Historical duplicate' }] }
    render()
    // 古い callback でも、次の layout を待たず最新の質問で新規送信を拒否する。
    oldStart({ selected_option_keys: ['a'], text: 'draft from the previous render' }); await microtasks()
    expect(respondToInteraction).not.toHaveBeenCalled()
    expect(render().pending).toBeNull()
  })

  it('confirms the frozen request after a refresh reveals duplicate historical choice keys', async () => {
    question = interactionFixture('CHOICE')
    vi.mocked(respondToInteraction).mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce({ ...interactionReceipt(), idempotent_replay: true })
    const hook = render(); commit(); hook.start({ selected_option_keys: ['a'], text: 'original' }); await microtasks()
    const first = vi.mocked(respondToInteraction).mock.calls[0]!
    question = { ...question, options: [...question.options, { key: 'a', label: 'Historical duplicate' }] }
    const unknown = render(); commit()
    expect(unknown.pending).toMatchObject({ phase: 'unknown', uncertain: true })
    unknown.confirmOriginal(); await microtasks()
    expect(vi.mocked(respondToInteraction).mock.calls[1]!.slice(0, 7)).toEqual(first.slice(0, 7))
    expect(render().pending?.receipt).toEqual({ ...interactionReceipt(), idempotent_replay: true })
  })
})
