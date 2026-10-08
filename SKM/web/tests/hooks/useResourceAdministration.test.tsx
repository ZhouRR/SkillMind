// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, type SecretReferenceRecord } from '../../src/api'
import { useResourceAdministration } from '../../src/hooks/useResourceAdministration'
import { RESOURCE_REQUEST_TIMEOUT_MS } from '../../src/hooks/useResourceRequest'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'

const api = vi.hoisted(() => ({
  loadSecretReferences: vi.fn(), loadIntegrations: vi.fn(), loadResourceBindings: vi.fn(),
  loadEffectPreauthorizations: vi.fn(), loadProjectTasks: vi.fn(),
}))
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), ...api }))

let root: Root
let container: HTMLDivElement
let state: ReturnType<typeof useResourceAdministration>
let session = 0
/** API の通信は mock のみ。実 hook の commit と遅延応答を観測する。 */
function Harness({ project = 'resource-project', sessionKey = `fixture-${session}` }: { project?: string; sessionKey?: string }) {
  state = useResourceAdministration(project, true, sessionKey)
  return null
}
/** Abort を無視する transport も試すため、完了を test が所有する。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((accept, refuse) => { resolve = accept; reject = refuse })
  return { promise, resolve, reject }
}
async function mount(props: Parameters<typeof Harness>[0] = {}) {
  await act(async () => root.render(<Harness {...props} />))
}
beforeEach(() => {
  session += 1
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  for (const loader of Object.values(api)) loader.mockReset().mockResolvedValue([])
  api.loadProjectTasks.mockResolvedValue({ tasks: [] })
})
afterEach(async () => {
  await act(async () => root.unmount()); container.remove(); vi.useRealTimers(); vi.unstubAllGlobals()
})

describe('resource administration catalog facts', () => {
  it('retranslates retained read failures without another resource request', async () => {
    api.loadIntegrations.mockRejectedValueOnce(new ApiProblemError('Synthetic administrator denial', 403, 'administrator_required'))
    await act(async () => root.render(<LanguageProvider language="en"><Harness /></LanguageProvider>))
    expect(state.loadError).toBe(MESSAGES.en.account.failures.adminRequired)
    await act(async () => root.render(<LanguageProvider language="ja"><Harness /></LanguageProvider>))
    expect(state.loadError).toBe(MESSAGES.ja.account.failures.adminRequired)
    expect(api.loadIntegrations).toHaveBeenCalledTimes(1)
    expect(api.loadSecretReferences).toHaveBeenCalledTimes(1)
  })

  it('does not turn initial pending or failed reads into a confirmed empty catalog, and retries read-only', async () => {
    const read = deferred<never[]>()
    api.loadIntegrations.mockReturnValueOnce(read.promise)
    await mount()
    expect(state.loading).toBe(true); expect(state.loaded).toBe(false)
    await act(async () => read.reject(new Error('fixture offline')))
    expect(state.loading).toBe(false); expect(state.loaded).toBe(false); expect(state.loadError).toContain('offline')
    await act(async () => state.refresh())
    expect(state.loaded).toBe(true); expect(state.integrations).toEqual([]); expect(state.loadError).toBeNull()
    expect(api.loadIntegrations).toHaveBeenCalledTimes(2)
  })
  it('keeps task load failure separate from an empty published task list', async () => {
    api.loadProjectTasks.mockRejectedValueOnce(new Error('fixture task failure'))
    await mount()
    expect(state.loaded).toBe(true); expect(state.taskStatus).toBe('error')
    expect(state.tasks).toEqual([]); expect(state.loadError).toBeNull()
    await act(async () => state.retryTasks())
    expect(state.taskStatus).toBe('ready'); expect(state.tasks).toEqual([])
    expect(api.loadIntegrations).toHaveBeenCalledTimes(1)
  })
  it('retains the last same-project catalog through a failed refresh but never across projects', async () => {
    api.loadIntegrations.mockResolvedValueOnce([{ integration_id: 'fixture-item', name: 'Original' }])
    await mount()
    api.loadIntegrations.mockRejectedValueOnce(new Error('fixture offline'))
    await act(async () => state.refresh())
    expect(state.loaded).toBe(true); expect(state.integrations[0]?.name).toBe('Original')
    const next = deferred<never[]>()
    api.loadIntegrations.mockReturnValueOnce(next.promise)
    await mount({ project: 'other-project' })
    expect(state.loaded).toBe(false); expect(state.integrations).toEqual([])
  })
  it('bounds a hanging read and does not accept its late response', async () => {
    vi.useFakeTimers()
    const read = deferred<never[]>()
    api.loadIntegrations.mockReturnValueOnce(read.promise)
    await mount()
    await act(async () => vi.advanceTimersByTime(RESOURCE_REQUEST_TIMEOUT_MS))
    expect(state.loading).toBe(false); expect(state.loadError).toBeTruthy(); expect(state.loaded).toBe(false)
    await act(async () => read.resolve([]))
    expect(state.loaded).toBe(false)
  })
})

describe('resource administration submitted-operation ownership', () => {
  it('stops observation, blocks duplicate writes, ignores late success, and requires a fresh read plus explicit review', async () => {
    await mount()
    const write = deferred<void>()
    const operation = vi.fn(() => write.promise)
    let completion!: Promise<boolean>
    await act(async () => {
      completion = state.perform('connect', operation, 'write', 'Submitted connection')
      expect(await state.perform('connect', operation)).toBe(false)
    })
    expect(operation).toHaveBeenCalledTimes(1); expect(state.busy).toBe('connect')
    await act(async () => state.stopWaiting())
    expect(await completion).toBe(false); expect(state.busy).toBeNull()
    expect(state.unconfirmed?.label).toBe('Submitted connection'); expect(state.canAcknowledge).toBe(false)
    expect(state.acknowledge()).toBe(false)
    await act(async () => write.resolve())
    expect(state.unconfirmed).not.toBeNull(); expect(api.loadIntegrations).toHaveBeenCalledTimes(1)
    await act(async () => state.refresh())
    expect(state.canAcknowledge).toBe(true)
    await act(async () => expect(await state.perform('connect', operation)).toBe(false))
    expect(operation).toHaveBeenCalledTimes(1)
    await act(async () => expect(state.acknowledge()).toBe(true))
    expect(state.unconfirmed).toBeNull()
  })
  it('times out unknown writes without retrying or waiting forever for an abort-ignoring transport', async () => {
    vi.useFakeTimers()
    await mount()
    const operation = vi.fn(() => new Promise<void>(() => {}))
    let completion!: Promise<boolean>
    await act(async () => { completion = state.perform('secret', operation, 'write', 'Submitted credential') })
    await act(async () => vi.advanceTimersByTime(RESOURCE_REQUEST_TIMEOUT_MS))
    expect(await completion).toBe(false)
    expect(state.unconfirmed?.key).toBe('secret'); expect(state.busy).toBeNull(); expect(operation).toHaveBeenCalledTimes(1)
  })
  it('treats explicit 409 as a known refusal and preserves a previously confirmed secret', async () => {
    await mount()
    const secret = { secret_reference_id: 'saved-secret', project_id: 'resource-project', name: 'Fixture' } as SecretReferenceRecord
    await act(async () => {
      await state.perform('connect', async () => {
        state.recordSecret(secret)
        throw new ApiProblemError('fixture conflict', 409)
      })
    })
    expect(state.unconfirmed).toBeNull(); expect(state.error).toContain('conflict')
    expect(state.secrets).toEqual([secret])
  })
  it.each([
    ['unreadable success', new ApiProblemError('bad response', 200)],
    ['transport failure', new Error('fixture network failure')],
    ['server failure', new ApiProblemError('fixture server failure', 500)],
    ['request timeout', new ApiProblemError('fixture timeout', 408)],
  ])('treats %s as unknown rather than safe-to-repeat refusal', async (_name, failure) => {
    await mount()
    await act(async () => { await state.perform('delete', async () => { throw failure }) })
    expect(state.unconfirmed?.key).toBe('delete')
    const nextWrite = vi.fn()
    await act(async () => expect(await state.perform('delete', nextWrite)).toBe(false))
    expect(nextWrite).not.toHaveBeenCalled()
  })
  it('retains an interrupted project request when switching back and isolates another signed-in session', async () => {
    await mount()
    await act(async () => { void state.perform('connect', () => new Promise<void>(() => {}), 'write', 'Private resource name') })
    await mount({ project: 'other-project' })
    expect(state.unconfirmed).toBeNull()
    await mount()
    expect(state.unconfirmed?.label).toBe('Private resource name')
    await mount({ sessionKey: 'another-fixture-session' })
    expect(state.unconfirmed).toBeNull()
    expect(state.error).toBeNull()
  })
})
