import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, deleteProjectDocument, loadProjectDocument, type ProjectDocumentRecord } from '../../src/api'
import { useDocumentDeletion } from '../../src/hooks/useDocumentDeletion'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  deleteProjectDocument: vi.fn(), loadProjectDocument: vi.fn() }))

const expired = vi.fn()
const refreshed = vi.fn()
const PROJECT = '10000000-0000-4000-8000-000000000001'

/** 同名でも別 ID の文書を作り、一覧の代替物で原対象を上書きできないことを確認する。 */
function document(index = 1): ProjectDocumentRecord {
  return { project_id: PROJECT, document_id: `20000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
    folder: '', name: 'notes.txt', size: 4, mime: 'text/plain', checksum: `sha256:${'a'.repeat(64)}`,
    uploaded_by: '30000000-0000-4000-8000-000000000001', created_at: '2026-09-10T00:00:00Z' }
}

/** actor/会話/Project 切替は実 component 同様に別 mount として扱う。 */
function render(readOnly = false) {
  hookPhases.cursor = 0
  return useDocumentDeletion({ projectId: PROJECT, csrfToken: 'test-csrf', readOnly,
    onSessionEnded: expired, onDeleted: refreshed })
}

/** Promise と effect だけを完了し、期限は各 test が明示して動かす。 */
async function settle(readOnly = false) {
  for (let index = 0; index < 4; index++) { await hookMicrotasks(); render(readOnly); commitHooks() }
  return render(readOnly)
}

/** DELETE の応答喪失から原 ID の人工照合が必要な状態を作る。 */
async function unknown() {
  vi.mocked(deleteProjectDocument).mockRejectedValueOnce(new Error('Connection lost'))
  render(); commitHooks(); expect(render().submit(document())).toBe(true)
  const current = await settle()
  expect(current.phase).toBe('unknown')
  expect(current.intent).toEqual(document())
  return current
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('document deletion ownership and original read', () => {
  it.each([401, 409, 500])('reports a stopped batch for DELETE %s without authorizing a retry', async (status) => {
    const settled = vi.fn()
    vi.mocked(deleteProjectDocument).mockRejectedValueOnce(new ApiProblemError('private', status,
      status === 401 ? 'authentication_required' : status === 409 ? 'document_in_use' : 'server_error'))
    render(); commitHooks()
    render().submit(document(), settled)
    await settle()
    expect(settled).toHaveBeenCalledExactlyOnceWith(false)
    expect(deleteProjectDocument).toHaveBeenCalledTimes(1)
    if (status === 500) expect(render().canWrite()).toBe(false)
  })

  it('accepts one DELETE synchronously and freezes the original ID before rerender', async () => {
    const response = deferred<void>()
    vi.mocked(deleteProjectDocument).mockReturnValue(response.promise)
    render(); commitHooks(); const current = render(); const selected = document()
    expect(current.submit(selected)).toBe(true)
    selected.name = 'Changed by caller'
    expect(current.submit(document(2))).toBe(false)
    expect(current.canWrite()).toBe(false); expect(current.canRead()).toBe(false)
    await settle()
    expect(deleteProjectDocument).toHaveBeenCalledExactlyOnceWith(PROJECT, document().document_id, 'test-csrf', expect.any(AbortSignal))
    expect(render().intent).toEqual(document())
    response.resolve(); const result = await settle()
    expect(result.phase).toBe('idle'); expect(result.intent).toBeNull()
    expect(result.canWrite()).toBe(true); expect(refreshed).toHaveBeenCalledTimes(1)
  })

  it.each(['present', 'absent'] as const)('requires explicit release after observing the original document as %s', async (status) => {
    const current = await unknown()
    if (status === 'present') vi.mocked(loadProjectDocument).mockResolvedValue(document())
    else vi.mocked(loadProjectDocument).mockRejectedValue(new ApiProblemError('private', 404, 'document_not_found'))
    current.checkOriginal(); let result = await settle()
    expect(loadProjectDocument).toHaveBeenCalledWith(PROJECT, document().document_id, expect.any(AbortSignal))
    expect(result.facts?.status).toBe(status)
    expect(result.phase).toBe('unknown'); expect(result.canWrite()).toBe(false)
    expect(refreshed).not.toHaveBeenCalled()
    result.release(); result.release(); result = await settle()
    expect(result.phase).toBe('idle'); expect(result.intent).toBeNull(); expect(result.facts).toBeNull()
    expect(result.canWrite()).toBe(true); expect(refreshed).toHaveBeenCalledTimes(1)
    expect(deleteProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('does not allow another original GET while the current check is waiting', async () => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(loadProjectDocument).mockReturnValue(response.promise)
    const current = await unknown(); current.checkOriginal(); current.checkOriginal()
    let result = await settle(); result.checkOriginal(); result = await settle()
    expect(loadProjectDocument).toHaveBeenCalledTimes(1)
    expect(result.checking).toBe(true)
    response.resolve(document()); await settle()
  })

  it.each([401, 403, 404] as const)('closes current read/write qualification on GET %s without hanging the check', async (status) => {
    vi.mocked(loadProjectDocument).mockRejectedValue(new ApiProblemError('private', status, 'project_not_found'))
    const current = await unknown(); current.checkOriginal(); let result = await settle()
    expect(result.denied?.key).toBe(status === 401 ? 'sessionExpired' : 'denied')
    expect(result.phase).toBe('unknown'); expect(result.intent).toEqual(document())
    expect(result.checking).toBe(false); expect(result.facts).toBeNull()
    expect(result.canWrite()).toBe(false); expect(result.canRead()).toBe(false)
    expect(expired).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
    result.release(); result.checkOriginal(); result = await settle()
    expect(result.phase).toBe('unknown'); expect(loadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it.each([401, 403, 404] as const)('does not apply GET %s side effects after the absolute deadline even before its timer fires', async (status) => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(loadProjectDocument).mockReturnValue(response.promise)
    const current = await unknown(); current.checkOriginal(); await settle()
    vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', status, 'project_not_found'))
    const result = await settle()
    expect(result.checkFailure).toEqual({ key: 'loadFailed' })
    expect(result.denied).toBeNull(); expect(expired).not.toHaveBeenCalled()
    expect(result.phase).toBe('unknown'); expect(result.facts).toBeNull()
    expect(vi.mocked(loadProjectDocument).mock.calls[0]![2]?.aborted).toBe(true)
  })

  it.each(['present', 'absent', '401'] as const)('discards a late %s after the read timer and permits only an explicit retry', async (outcome) => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(loadProjectDocument).mockReturnValueOnce(response.promise).mockResolvedValueOnce(document())
    const current = await unknown(); current.checkOriginal(); await settle()
    vi.advanceTimersByTime(30_000)
    if (outcome === 'present') response.resolve(document())
    else response.reject(new ApiProblemError('private', outcome === '401' ? 401 : 404, 'document_not_found'))
    let result = await settle()
    expect(result.checkFailure).toEqual({ key: 'loadFailed' }); expect(result.denied).toBeNull()
    expect(result.facts).toBeNull(); expect(result.phase).toBe('unknown'); expect(expired).not.toHaveBeenCalled()
    expect(loadProjectDocument).toHaveBeenCalledTimes(1)
    result.checkOriginal(); result = await settle()
    expect(result.facts?.status).toBe('present'); expect(result.phase).toBe('unknown')
    expect(deleteProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('notifies expiry once for concurrent qualified reads owned by the same document panel', async () => {
    render(); commitHooks(); const current = render()
    current.observeFailure(new ApiProblemError('private', 401))
    current.observeFailure(new ApiProblemError('private', 401))
    current.observeDenial({ key: 'sessionExpired' })
    expect(expired).toHaveBeenCalledTimes(1)
    expect((await settle()).canWrite()).toBe(false)
  })

  it('ends the wait on a definitive DELETE 401 while keeping the owner denied', async () => {
    vi.mocked(deleteProjectDocument).mockRejectedValue(new ApiProblemError('private', 401))
    render(); commitHooks(); render().submit(document()); const result = await settle()
    expect(result.phase).toBe('idle'); expect(result.intent).toBeNull()
    expect(result.denied?.key).toBe('sessionExpired'); expect(result.canWrite()).toBe(false)
    expect(expired).toHaveBeenCalledTimes(1); expect(refreshed).not.toHaveBeenCalled()
  })

  it('interrupts an in-flight DELETE after another current read denial without inventing a rollback', async () => {
    const response = deferred<void>()
    vi.mocked(deleteProjectDocument).mockReturnValue(response.promise)
    render(); commitHooks(); render().submit(document()); await settle()
    render().observeDenial({ key: 'denied' }); response.resolve(); const result = await settle()
    expect(result.phase).toBe('unknown'); expect(result.intent).toEqual(document())
    expect(result.denied?.key).toBe('denied'); expect(result.canWrite()).toBe(false)
    expect(refreshed).not.toHaveBeenCalled()
  })

  it('permits archived original metadata review but never reopens writes', async () => {
    const current = await unknown(); vi.mocked(loadProjectDocument).mockResolvedValue(document())
    render(true); commitHooks(); current.checkOriginal(); let result = await settle(true)
    expect(result.facts?.status).toBe('present'); result.release(); result = await settle(true)
    expect(result.phase).toBe('idle'); expect(result.canRead()).toBe(true); expect(result.canWrite()).toBe(false)
    expect(result.submit(document(2))).toBe(false); expect(deleteProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('keeps archived GET available after a definitive write refusal', async () => {
    vi.mocked(deleteProjectDocument).mockRejectedValue(new ApiProblemError('private', 409, 'project_archived'))
    render(); commitHooks(); render().submit(document()); const result = await settle()
    expect(result.phase).toBe('idle'); expect(result.denied?.key).toBe('archived')
    expect(result.readDenied).toBe(false); expect(result.canRead()).toBe(true); expect(result.canWrite()).toBe(false)
  })

  it('allows only original review and explicit release when archival interrupts an in-flight DELETE', async () => {
    const response = deferred<void>()
    vi.mocked(deleteProjectDocument).mockReturnValue(response.promise)
    vi.mocked(loadProjectDocument).mockResolvedValue(document())
    render(); commitHooks(); render().submit(document()); await settle()
    render().observeDenial({ key: 'archived' }); response.resolve()
    let result = await settle(); result.checkOriginal(); result = await settle()
    expect(result.phase).toBe('unknown'); expect(result.facts?.status).toBe('present')
    expect(result.readDenied).toBe(false); expect(result.canWrite()).toBe(false)
    expect(refreshed).not.toHaveBeenCalled()
    result.release(); result = await settle()
    expect(result.phase).toBe('idle'); expect(result.canRead()).toBe(true); expect(result.canWrite()).toBe(false)
    expect(deleteProjectDocument).toHaveBeenCalledTimes(1)
  })

  it.each(['denied', 'sessionExpired'] as const)('does not replace the stronger %s refusal with archival', async (key) => {
    render(); commitHooks(); const current = render()
    current.observeDenial({ key }); current.observeDenial({ key: 'archived' })
    const result = await settle()
    expect(result.denied?.key).toBe(key); expect(result.canRead()).toBe(false); expect(result.canWrite()).toBe(false)
    expect(expired).toHaveBeenCalledTimes(key === 'sessionExpired' ? 1 : 0)
  })

  it.each(['present', '401'] as const)('does not apply old owner %s responses or callbacks after unmount', async (outcome) => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(loadProjectDocument).mockReturnValue(response.promise)
    const current = await unknown(); current.checkOriginal(); const previous = await settle()
    unmountHooks(); render(); commitHooks()
    if (outcome === 'present') response.resolve(document()); else response.reject(new ApiProblemError('private', 401))
    previous.observeDenial({ key: 'sessionExpired' }); previous.checkOriginal(); previous.release()
    expect(previous.canRead()).toBe(false); expect(previous.canWrite()).toBe(false)
    const result = await settle()
    expect(result.phase).toBe('idle'); expect(result.denied).toBeNull(); expect(result.facts).toBeNull()
    expect(expired).not.toHaveBeenCalled(); expect(refreshed).not.toHaveBeenCalled()
  })
})
