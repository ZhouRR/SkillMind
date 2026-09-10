import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadDocumentUpload, uploadProjectDocument, type DocumentUploadBody,
  type DocumentUploadRecord, type ProjectDocumentRecord } from '../../src/api'
import { useDocumentUpload } from '../../src/hooks/useDocumentUpload'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  loadDocumentUpload: vi.fn(), uploadProjectDocument: vi.fn() }))

const ACTOR = '00000000-0000-4000-8000-000000000030'
const PROJECT = '00000000-0000-4000-8000-000000000020'
const KEY = '00000000-0000-4000-8000-000000000911'
const OTHER_KEY = '00000000-0000-4000-8000-000000000912'
const denied = vi.fn()
const published = vi.fn()
const storage = { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn(), clear: vi.fn() }
type Options = Partial<Parameters<typeof useDocumentUpload>[0]>

/** 同じ owner の hook を描画する。実 DOM の key/remount は別の component/browser 回帰で扱う。 */
function render(overrides: Options = {}) {
  hookPhases.cursor = 0
  return useDocumentUpload({ actorId: ACTOR, projectId: PROJECT, csrfToken: 'synthetic-session',
    readOnly: false, canRead: () => true, canWrite: () => true, onDenied: denied,
    onPublished: published, ...overrides })
}

/** Timer や retry を進めず、共有 request hook と所有者 effect の更新だけを commit する。 */
async function settle(overrides: Options = {}) {
  for (let index = 0; index < 5; index++) { await hookMicrotasks(); render(overrides); commitHooks() }
  return render(overrides)
}

/** 実 File を用い、原 multipart に残る bytes と MIME を検証できるようにする。 */
function file(name = 'first.md', content = 'fixed original bytes'): File {
  return new File([content], name, { type: 'text/markdown', lastModified: 1234 })
}

/** Transport が受けた原 body に一致する公開 metadata を作る。 */
function document(body?: DocumentUploadBody): ProjectDocumentRecord {
  return { document_id: '00000000-0000-4000-8000-000000000040', project_id: PROJECT,
    folder: body?.folder ?? '', name: body?.name ?? 'first.md', size: body?.file.size ?? 20,
    mime: body?.file.type ?? 'text/markdown', checksum: `sha256:${'a'.repeat(64)}`,
    uploaded_by: ACTOR, created_at: '2026-09-01T01:00:00Z' }
}

/** 書込を伴わない原 key の未公開受付記録を作る。 */
function pending(key = KEY): DocumentUploadRecord {
  return { upload_key: key, project_id: PROJECT, created_at: '2026-09-01T00:00:00Z',
    state: 'PENDING', document: null }
}

/** 手動 GET で確認できる公開済み受付記録を作る。 */
function receipt(key = KEY, value = document()): DocumentUploadRecord {
  return { ...pending(key), state: 'PUBLISHED', document: value }
}

/** 本物の POST 境界を一度失敗させ、後続 File を未送信のまま残す。 */
async function unknown(files = [file(), file('second.md')], overrides: Options = {}) {
  vi.mocked(uploadProjectDocument).mockRejectedValueOnce(new TypeError('synthetic network failure'))
  const hook = render(overrides); commitHooks()
  expect(hook.start(files)).toBe(true)
  const current = await settle(overrides)
  expect(current.batch.items[0]?.phase).toBe('unknown')
  return current
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: storage, sessionStorage: storage })
  vi.stubGlobal('localStorage', storage)
  vi.stubGlobal('sessionStorage', storage)
  vi.mocked(uploadProjectDocument).mockImplementation(async (_project, _key, body) => document(body))
  vi.mocked(loadDocumentUpload).mockImplementation(async (_project, key) => pending(key))
})

afterEach(() => {
  unmountHooks()
  expect(storage.setItem).not.toHaveBeenCalled()
  expect(storage.removeItem).not.toHaveBeenCalled()
  expect(storage.clear).not.toHaveBeenCalled()
  vi.restoreAllMocks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals()
})

describe('document manual read-only recovery', () => {
  it.each([
    ['not found', new ApiProblemError('private', 404, 'document_upload_not_found'), 'uploadNotFound'],
    ['unavailable', new ApiProblemError('private', 503, 'document_upload_unavailable'), 'uploadUnavailable'],
    ['invalid transport record', new Error('contract rejected private response'), 'loadFailed'],
  ] as const)('can close %s and query a different key without creating an upload', async (_label, error, failure) => {
    vi.mocked(loadDocumentUpload).mockRejectedValueOnce(error).mockResolvedValueOnce(receipt(OTHER_KEY))
    const hook = render(); commitHooks(); hook.recover(KEY)
    let current = await settle()
    expect(current.recovery).toMatchObject({ phase: 'settled', original: { uploadKey: KEY, body: null }, failure: { key: failure } })
    expect(current.batch).toEqual({ items: [], paused: false })
    expect(current.canStart()).toBe(true)
    current.closeRecovery(); current = render()
    expect(current.recovery).toBeNull()
    current.recover(OTHER_KEY); current = await settle()
    expect(current.recovery).toMatchObject({ phase: 'settled', record: { state: 'PUBLISHED', upload_key: OTHER_KEY } })
    expect(loadDocumentUpload).toHaveBeenCalledTimes(2)
    expect(uploadProjectDocument).not.toHaveBeenCalled()
    expect(published).not.toHaveBeenCalled()
  })

  it('can close or replace a PENDING manual key without acknowledging a write', async () => {
    const hook = render(); commitHooks(); hook.recover(KEY)
    let current = await settle()
    expect(current.recovery).toMatchObject({ phase: 'settled', record: { state: 'PENDING' }, failure: null })
    expect(current.isLocked()).toBe(false)
    current.recover(OTHER_KEY); current = await settle()
    expect(current.recovery?.original.uploadKey).toBe(OTHER_KEY)
    current.closeRecovery(); current = render()
    expect(current.recovery).toBeNull()
    expect(current.batch.items).toEqual([])
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })

  it.each(['not-a-uuid', '00000000-0000-0000-0000-000000000000'])('rejects invalid manual key %s without touching files or dispatch', async (key) => {
    const current = await unknown()
    const original = current.batch
    current.recover(key)
    expect(render().notice).toEqual({ key: 'uploadInvalidKey' })
    expect(render().batch).toBe(original)
    expect(render().recovery).toBeNull()
    expect(loadDocumentUpload).not.toHaveBeenCalled()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('ignores duplicate same-tick same-key GET and replaces a pending different key synchronously', async () => {
    const first = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUpload).mockReturnValueOnce(first.promise).mockResolvedValueOnce(pending(OTHER_KEY))
    const hook = render(); commitHooks(); hook.recover(KEY); hook.recover(KEY)
    let current = await settle()
    expect(loadDocumentUpload).toHaveBeenCalledTimes(1)
    current.recover(OTHER_KEY)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    first.reject(new ApiProblemError('private', 401)); await hookMicrotasks()
    expect(denied).not.toHaveBeenCalled()
    current = await settle()
    expect(current.recovery?.record?.upload_key).toBe(OTHER_KEY)
    expect(loadDocumentUpload).toHaveBeenCalledTimes(2)
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })

  it('dispatches only the final manual key when two different keys are entered before commit', async () => {
    const hook = render(); commitHooks(); hook.recover(KEY); hook.recover(OTHER_KEY)
    const current = await settle()
    expect(loadDocumentUpload).toHaveBeenCalledTimes(1)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]!.slice(0, 2)).toEqual([PROJECT, OTHER_KEY])
    expect(current.recovery?.record?.upload_key).toBe(OTHER_KEY)
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })

  it.each(['close', 'cancel'] as const)('%s aborts manual GET before a same-tick late 401', async (action) => {
    const response = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.recover(KEY)
    let current = await settle()
    if (action === 'close') current.closeRecovery(); else current.cancelCheck()
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    response.reject(new ApiProblemError('private', 401)); await hookMicrotasks()
    expect(denied).not.toHaveBeenCalled()
    current = await settle()
    if (action === 'close') expect(current.recovery).toBeNull()
    else expect(current.recovery).toMatchObject({ phase: 'cancelled', record: null, failure: null })
    expect(current.canRecover()).toBe(true)
    expect(current.canStart()).toBe(true)
  })

  it('cancels a manual GET synchronously when a valid new file batch starts', async () => {
    const response = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.recover(KEY)
    const current = await settle()
    expect(current.start([file()])).toBe(true)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    response.reject(new ApiProblemError('private', 401)); await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(render().recovery).toBeNull()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(published).toHaveBeenCalledTimes(1)
  })

  it.each(['timeout', 'delayed timer'] as const)('makes manual GET %s an exit-able read failure and ignores its late 401', async (mode) => {
    const response = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.recover(KEY)
    await settle()
    if (mode === 'timeout') vi.advanceTimersByTime(30_000)
    else vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401))
    const current = await settle()
    expect(current.recovery).toMatchObject({ phase: 'settled', failure: { key: 'loadFailed' } })
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    expect(denied).not.toHaveBeenCalled()
    current.closeRecovery()
    expect(render().recovery).toBeNull()
    expect(render().isLocked()).toBe(false)
  })

  it('rejects a structurally valid foreign-actor manual receipt without treating it as upload confirmation', async () => {
    vi.mocked(loadDocumentUpload).mockResolvedValue(receipt(KEY, { ...document(), uploaded_by: PROJECT }))
    const hook = render(); commitHooks(); hook.recover(KEY)
    const current = await settle()
    expect(current.recovery).toMatchObject({ record: null, failure: { key: 'loadFailed' } })
    expect(published).not.toHaveBeenCalled()
    expect(uploadProjectDocument).not.toHaveBeenCalled()
    current.closeRecovery()
    expect(render().canRecover()).toBe(true)
  })
})

describe('document original POST ownership', () => {
  it('accepts one same-tick file selection and one original-key GET without replacing the original batch', async () => {
    vi.mocked(uploadProjectDocument).mockRejectedValue(new TypeError('network'))
    const hook = render(); commitHooks()
    expect(hook.start([file()])).toBe(true)
    expect(hook.start([file('replacement.md')])).toBe(false)
    let current = await settle()
    const original = current.batch.items[0]!.original
    current.checkOriginal(); current.checkOriginal(); current = await settle()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(loadDocumentUpload).toHaveBeenCalledTimes(1)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]!.slice(0, 2)).toEqual([PROJECT, original.uploadKey])
    expect(current.batch.items[0]!.original).toBe(original)
    expect(current.pendingReceipt).toBe(true)
  })

  it.each(['PENDING', 'PUBLISHED', '404', '503', 'invalid'] as const)(
    'manual %s, exit, and a different manual key cannot release real POST unknown or discard queued Files', async (state) => {
      const selected = file('first.md', 'immutable selected content')
      Object.defineProperty(selected, 'webkitRelativePath', { configurable: true, value: 'folder/first.md' })
      let current = await unknown([selected, file('second.md')])
      const before = current.batch
      const original = before.items[0]!.original
      Object.defineProperty(selected, 'webkitRelativePath', { value: 'changed/name.md' })
      if (state === 'PENDING') vi.mocked(loadDocumentUpload).mockResolvedValueOnce(pending(original.uploadKey))
      else if (state === 'PUBLISHED') vi.mocked(loadDocumentUpload).mockResolvedValueOnce(receipt(original.uploadKey, document(original.body!)))
      else vi.mocked(loadDocumentUpload).mockRejectedValueOnce(state === 'invalid' ? new Error('invalid contract')
        : new ApiProblemError('private', Number(state), state === '404' ? 'document_upload_not_found' : 'document_upload_unavailable'))
      current.recover(original.uploadKey); current = await settle()
      expect(current.batch).toBe(before)
      expect(current.canContinue()).toBe(false)
      current.continueBatch(); current.closeRecovery()
      expect(current.start([file('new-key.md')])).toBe(false)
      current.recover(OTHER_KEY); current = await settle(); current.closeRecovery()
      expect(current.batch).toBe(before)
      expect(current.batch.items.map((item) => item.phase)).toEqual(['unknown', 'queued'])
      expect(current.batch.items[1]!.original.body).toBe(before.items[1]!.original.body)
      expect(original.body).toMatchObject({ folder: 'folder', name: 'first.md' })
      expect(await original.body!.file.text()).toBe('immutable selected content')
      expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
      expect(published).not.toHaveBeenCalled()
      expect(current.isLocked()).toBe(true)
    },
  )

  it.each(['404', '503', 'invalid', 'wrong actor', 'wrong size'] as const)('keeps original GET %s unknown and does not send its queued successor', async (failure) => {
    let current = await unknown()
    const original = current.batch.items[0]!.original
    if (failure === 'wrong actor' || failure === 'wrong size') {
      const value = document(original.body!)
      vi.mocked(loadDocumentUpload).mockResolvedValue(receipt(original.uploadKey,
        failure === 'wrong actor' ? { ...value, uploaded_by: PROJECT } : { ...value, size: value.size + 1 }))
    } else vi.mocked(loadDocumentUpload).mockRejectedValue(failure === 'invalid' ? new Error('invalid contract')
      : new ApiProblemError('private', Number(failure), failure === '404' ? 'document_upload_not_found' : 'document_upload_unavailable'))
    current.checkOriginal(); current = await settle()
    expect(current.batch.items.map((item) => item.phase)).toEqual(['unknown', 'queued'])
    expect(current.batch.items[0]!.original).toBe(original)
    expect(current.checkFailure).not.toBeNull()
    expect(current.canContinue()).toBe(false)
    current.closeRecovery(); current.continueBatch()
    expect(current.start([file('replacement.md')])).toBe(false)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(published).not.toHaveBeenCalled()
  })

  it('requires explicit continue after the original PUBLISHED receipt and then sends only the queued original File', async () => {
    let current = await unknown()
    const first = current.batch.items[0]!.original
    const second = current.batch.items[1]!.original
    vi.mocked(loadDocumentUpload).mockResolvedValue(receipt(first.uploadKey, document(first.body!)))
    current.checkOriginal(); current = await settle()
    expect(current.batch.items.map((item) => item.phase)).toEqual(['published', 'queued'])
    expect(current.batch.paused).toBe(true)
    expect(current.isLocked()).toBe(true)
    expect(current.canContinue()).toBe(true)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(current.start([file('replacement.md')])).toBe(false)
    expect(published).toHaveBeenCalledTimes(1)
    current.continueBatch(); current.continueBatch(); current = await settle()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(2)
    expect(vi.mocked(uploadProjectDocument).mock.calls[1]!.slice(0, 3)).toEqual([PROJECT, second.uploadKey, second.body])
    expect(vi.mocked(uploadProjectDocument).mock.calls[1]![2]).toBe(second.body)
    expect(current.batch.items.map((item) => item.phase)).toEqual(['published', 'published'])
    expect(published).toHaveBeenCalledTimes(2)
  })

  it.each(['cancel', 'timeout', 'delayed timer'] as const)('POST %s retains an unknown original request and ignores late 401', async (mode) => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(uploadProjectDocument).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start([file(), file('second.md')])
    let current = await settle()
    const original = current.batch.items[0]!.original
    if (mode === 'cancel') current.cancelUpload()
    else if (mode === 'timeout') vi.advanceTimersByTime(30_000)
    else vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401)); current = await settle()
    expect(current.batch.items.map((item) => item.phase)).toEqual(['unknown', 'queued'])
    expect(current.batch.items[0]!.original).toBe(original)
    expect(current.batch.items[0]!.failure).toEqual({ key: 'uploadUnknown' })
    expect(vi.mocked(uploadProjectDocument).mock.calls[0]![4]?.aborted).toBe(true)
    expect(denied).not.toHaveBeenCalled()
    expect(published).not.toHaveBeenCalled()
    current.closeRecovery(); current.continueBatch()
    expect(current.start([file()])).toBe(false)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('cancels original GET synchronously and ignores late publication without changing the unknown batch', async () => {
    const response = deferred<DocumentUploadRecord>()
    let current = await unknown()
    const before = current.batch
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    current.checkOriginal(); current = await settle(); current.cancelCheck()
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    response.resolve(receipt(before.items[0]!.original.uploadKey, document(before.items[0]!.original.body!)))
    current = await settle()
    expect(current.batch).toBe(before)
    expect(current.canContinue()).toBe(false)
    expect(published).not.toHaveBeenCalled()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it.each(['cancel', 'timeout', 'delayed timer'] as const)('original GET %s ignores late 401 and cannot release the unknown batch', async (mode) => {
    const response = deferred<DocumentUploadRecord>()
    let current = await unknown()
    const before = current.batch
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    current.checkOriginal(); current = await settle()
    if (mode === 'cancel') current.cancelCheck()
    else if (mode === 'timeout') vi.advanceTimersByTime(30_000)
    else vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401)); current = await settle()
    expect(current.batch).toBe(before)
    expect(current.canContinue()).toBe(false)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    if (mode !== 'cancel') expect(current.checkFailure).toEqual({ key: 'loadFailed' })
    expect(denied).not.toHaveBeenCalled()
    expect(published).not.toHaveBeenCalled()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('can switch a completed original PENDING check to manual lookup while preserving its unknown File batch', async () => {
    let current = await unknown()
    const before = current.batch
    current.checkOriginal(); current = await settle()
    expect(current.pendingReceipt).toBe(true)
    expect(current.canRecover()).toBe(true)
    current.recover(OTHER_KEY); current = await settle(); current.closeRecovery()
    expect(current.batch).toBe(before)
    expect(current.canContinue()).toBe(false)
    expect(current.start([file()])).toBe(false)
    expect(loadDocumentUpload).toHaveBeenCalledTimes(2)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('preserves same-owner files and an active manual GET through unrelated list refreshes', async () => {
    const response = deferred<DocumentUploadRecord>()
    let current = await unknown()
    const before = current.batch
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    current.recover(KEY); current = await settle()
    const original = current.recovery!.original
    current = await settle()
    expect(current.batch).toBe(before)
    expect(current.recovery?.original).toBe(original)
    expect(current.recovery?.phase).toBe('checking')
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(false)
    response.resolve(pending()); current = await settle()
    expect(current.batch).toBe(before)
    expect(loadDocumentUpload).toHaveBeenCalledTimes(1)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('does not let manual close cancel a genuine batch check or permit a competing manual request', async () => {
    const response = deferred<DocumentUploadRecord>()
    let current = await unknown()
    const original = current.batch.items[0]!.original
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    current.checkOriginal(); current = await settle()
    expect(current.canRecover()).toBe(false)
    current.closeRecovery(); current.recover(OTHER_KEY)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(false)
    response.resolve(receipt(original.uploadKey, document(original.body!))); current = await settle()
    expect(loadDocumentUpload).toHaveBeenCalledTimes(1)
    expect(current.batch.items[0]!.phase).toBe('published')
    expect(current.canContinue()).toBe(true)
  })
})

describe('document recovery authorization and owner boundaries', () => {
  it('current parallel manual GET 401 aborts POST but retains its original unknown bytes and queued successor', async () => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(uploadProjectDocument).mockReturnValue(response.promise)
    vi.mocked(loadDocumentUpload).mockRejectedValue(new ApiProblemError('private', 401))
    const hook = render(); commitHooks(); hook.start([file(), file('second.md')])
    let current = await settle()
    const first = current.batch.items[0]!.original
    const second = current.batch.items[1]!.original
    current.recover(KEY); current = await settle()
    expect(current.batch.items.map((item) => item.phase)).toEqual(['unknown', 'queued'])
    expect(current.batch.items[0]!.original).toBe(first)
    expect(current.batch.items[1]!.original).toBe(second)
    expect(vi.mocked(uploadProjectDocument).mock.calls[0]![4]?.aborted).toBe(true)
    expect(denied).toHaveBeenCalledExactlyOnceWith({ key: 'sessionExpired' })
    response.resolve(document(first.body!)); current = await settle()
    current.closeRecovery(); current.continueBatch()
    expect(current.start([file()])).toBe(false)
    expect(published).not.toHaveBeenCalled()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(denied).toHaveBeenCalledTimes(1)
  })

  it('does not dispatch manual or original reads when the shared read gate is already closed', async () => {
    let current = await unknown()
    const before = current.batch
    current = render({ canRead: () => false, canWrite: () => false }); commitHooks()
    current.recover(KEY); current.checkOriginal()
    expect(current.canRecover()).toBe(false)
    expect(current.start([file()])).toBe(false)
    await hookMicrotasks()
    expect(render({ canRead: () => false, canWrite: () => false }).batch).toBe(before)
    expect(loadDocumentUpload).not.toHaveBeenCalled()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it.each([
    [401, undefined, 'sessionExpired'], [403, undefined, 'denied'], [404, 'project_not_found', 'denied'],
  ] as const)('current GET %s denies once and cannot be cleared by closing a manual query', async (status, code, key) => {
    vi.mocked(loadDocumentUpload).mockRejectedValue(new ApiProblemError('private', status, code))
    const hook = render(); commitHooks(); hook.recover(KEY)
    let current = await settle()
    expect(current.denied).toEqual({ key })
    expect(denied).toHaveBeenCalledExactlyOnceWith({ key })
    current.closeRecovery(); current.recover(OTHER_KEY); current = await settle()
    expect(current.canRecover()).toBe(false)
    expect(current.start([file()])).toBe(false)
    expect(loadDocumentUpload).toHaveBeenCalledTimes(1)
    expect(uploadProjectDocument).not.toHaveBeenCalled()
    expect(published).not.toHaveBeenCalled()
  })

  it('allows archived manual lookup without reconstructing a file or granting writes', async () => {
    const options = { readOnly: true, canWrite: () => false }
    vi.mocked(loadDocumentUpload).mockResolvedValue(receipt())
    const hook = render(options); commitHooks()
    expect(hook.start([file()])).toBe(false)
    hook.recover(KEY)
    const current = await settle(options)
    expect(current.recovery).toMatchObject({ phase: 'settled', original: { body: null }, record: { state: 'PUBLISHED' } })
    expect(current.canRecover()).toBe(true)
    expect(current.canContinue()).toBe(false)
    current.continueBatch(); current.closeRecovery()
    expect(current.start([file()])).toBe(false)
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })

  it('permits original confirmation after archiving but cannot continue a queued file', async () => {
    let current = await unknown()
    const original = current.batch.items[0]!.original
    const options = { readOnly: true, canWrite: () => false }
    vi.mocked(loadDocumentUpload).mockResolvedValue(receipt(original.uploadKey, document(original.body!)))
    current = render(options); commitHooks(); current.checkOriginal(); current = await settle(options)
    expect(current.batch.items.map((item) => item.phase)).toEqual(['published', 'queued'])
    expect(current.canContinue()).toBe(false)
    current.continueBatch()
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(published).toHaveBeenCalledTimes(1)
  })

  it('can release a confirmed final original item while archived without sending new bytes', async () => {
    let current = await unknown([file()])
    const original = current.batch.items[0]!.original
    const options = { readOnly: true, canWrite: () => false }
    vi.mocked(loadDocumentUpload).mockResolvedValue(receipt(original.uploadKey, document(original.body!)))
    current = render(options); commitHooks(); current.checkOriginal(); current = await settle(options)
    expect(current.canContinue()).toBe(true)
    current.continueBatch(); current = await settle(options)
    expect(current.isLocked()).toBe(false)
    expect(current.start([file()])).toBe(false)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })

  it('rejects an in-flight manual publication when the shared read gate is lost', async () => {
    const response = deferred<DocumentUploadRecord>()
    let readable = true
    const options = { canRead: () => readable }
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    const hook = render(options); commitHooks(); hook.recover(KEY); await settle(options)
    readable = false
    response.resolve(receipt()); const current = await settle(options)
    expect(current.recovery).toMatchObject({ phase: 'cancelled', record: null })
    expect(current.canRecover()).toBe(false)
    expect(published).not.toHaveBeenCalled()
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })

  it.each(['actor', 'project', 'session'] as const)('ignores old GET 401 when the %s owner is actually unmounted', async (scope) => {
    const response = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.recover(KEY); await settle()
    unmountHooks()
    const options = scope === 'session' ? { csrfToken: 'new-valid-session' }
      : scope === 'actor' ? { actorId: OTHER_KEY } : { projectId: OTHER_KEY }
    render(options); commitHooks()
    response.reject(new ApiProblemError('private', 401)); const current = await settle(options)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]![2]?.aborted).toBe(true)
    expect(current.recovery).toBeNull()
    expect(current.batch.items).toEqual([])
    expect(denied).not.toHaveBeenCalled()
  })

  it('a new valid session can manually read the original key without retaining or reposting its lost multipart', async () => {
    const response = deferred<ProjectDocumentRecord>()
    vi.mocked(uploadProjectDocument).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); hook.start([file()]); const sending = await settle()
    const original = sending.batch.items[0]!.original
    unmountHooks()
    const options = { csrfToken: 'new-valid-session' }
    vi.mocked(loadDocumentUpload).mockResolvedValue(receipt(original.uploadKey, document(original.body!)))
    const replacement = render(options); commitHooks(); replacement.recover(original.uploadKey)
    response.reject(new ApiProblemError('private', 401)); const current = await settle(options)
    expect(current.recovery).toMatchObject({ original: { uploadKey: original.uploadKey, body: null }, record: { state: 'PUBLISHED' } })
    expect(current.batch.items).toEqual([])
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(denied).not.toHaveBeenCalled()
    expect(published).not.toHaveBeenCalled()
  })
})
