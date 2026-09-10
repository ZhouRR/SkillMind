import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, closeDocumentUpload, loadDocumentUploadClosure, loadDocumentUpload, uploadProjectDocument,
  type DocumentUploadClosureReceipt, type DocumentUploadBody, type DocumentUploadRecord } from '../../src/api'
import { useDocumentUpload } from '../../src/hooks/useDocumentUpload'
import { useDocumentUploadClosure } from '../../src/hooks/useDocumentUploadClosure'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  closeDocumentUpload: vi.fn(), loadDocumentUploadClosure: vi.fn(), uploadProjectDocument: vi.fn(), loadDocumentUpload: vi.fn() }))
const ACTOR = '00000000-0000-4000-8000-000000000030'
const PROJECT = '00000000-0000-4000-8000-000000000020'
const KEY = '00000000-0000-4000-8000-000000000080'
const OTHER = '00000000-0000-4000-8000-000000000081'
const DOCUMENT = '00000000-0000-4000-8000-000000000090'
const denied = vi.fn()
const published = vi.fn()
let readOnly = false
let accessible = true
let csrf = 'synthetic-session'

/** 実 upload と停止 hook を Panel と同じ同期 port で接続する。 */
function render() {
  hookPhases.cursor = 0
  let locked = () => false
  let endLookup = () => {}
  const upload = useDocumentUpload({ actorId: ACTOR, projectId: PROJECT, csrfToken: csrf,
    readOnly, canRead: () => accessible, canWrite: () => accessible && !locked(), onDenied: denied, onPublished: published,
    beforeBatchAction: () => endLookup() })
  const closure = useDocumentUploadClosure({ actorId: ACTOR, projectId: PROJECT, csrfToken: csrf,
    readOnly: readOnly || !!upload.denied, canRead: upload.canRead, canWrite: () => accessible && !upload.denied,
    claim: upload.claimClosure, claimRecovery: upload.claimRecoveredClosure,
    release: upload.releaseClosure, accept: upload.acceptClosure, acceptRecovery: upload.acceptRecoveredClosure,
    beforeAction: upload.closeRecovery, onDenied: upload.observeDenial })
  locked = closure.locked
  endLookup = closure.recovery.close
  return { upload, closure }
}
/** Browser timer を進めずに共通 hook の commit と Promise だけを収束させる。 */
async function settle() {
  for (let index = 0; index < 6; index++) { await hookMicrotasks(); render(); commitHooks() }
  return render()
}
/** 停止の正規受付記録は元 key と Project にだけ結び付く。 */
function receipt(key = KEY): DocumentUploadClosureReceipt {
  return { project_id: PROJECT, upload_key: key, document_id: DOCUMENT,
    publication_state: 'CLOSED', closed_at: '2026-09-10T00:00:00Z' }
}
/** 実送信内容と一致する公開 document を返す。 */
function document(body: DocumentUploadBody) {
  return { project_id: PROJECT, document_id: DOCUMENT, uploaded_by: ACTOR, folder: body.folder ?? '',
    name: body.name ?? body.file.name, size: body.file.size, mime: body.file.type,
    checksum: `sha256:${'a'.repeat(64)}`, created_at: '2026-09-10T00:00:00Z' }
}
/** 原 file の一回の POST を未知にし、後続 file を未送信で残す。 */
async function unknown() {
  vi.mocked(uploadProjectDocument).mockRejectedValueOnce(new TypeError('synthetic network failure'))
  const current = render(); commitHooks()
  current.upload.start([new File(['original'], 'first.md'), new File(['queued'], 'second.md')])
  const next = await settle()
  expect(next.upload.batch.items.map((item) => item.phase)).toEqual(['unknown', 'queued'])
  return next
}
/** 人工確認を通して一つだけ停止 POST を開始する。 */
async function stopping() {
  const current = await unknown()
  current.closure.prepare(current.upload.batch.items[0]!.original)
  const prepared = render(); commitHooks(); prepared.closure.submit(); prepared.closure.submit()
  return settle()
}
beforeEach(() => {
  vi.resetAllMocks(); readOnly = false; accessible = true; csrf = 'synthetic-session'
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('window', { setTimeout, clearTimeout, setInterval, clearInterval })
  vi.mocked(uploadProjectDocument).mockImplementation(async (_project, _key, body) => document(body))
  vi.mocked(closeDocumentUpload).mockImplementation(async (_project, key) => receipt(key))
  vi.mocked(loadDocumentUploadClosure).mockImplementation(async (_project, key) => receipt(key))
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('original publication closure and upload batch ownership', () => {
  it.each(['success', 'unknown'] as const)('can close only a current recovered PENDING original without fabricating a batch: %s', async (mode) => {
    vi.mocked(loadDocumentUpload).mockResolvedValue({ project_id: PROJECT, upload_key: KEY,
      created_at: '2026-09-10T00:00:00Z', state: 'PENDING', document: null })
    if (mode === 'unknown') vi.mocked(closeDocumentUpload).mockRejectedValueOnce(new TypeError('private'))
    render(); commitHooks(); let current = render()
    current.upload.recover(KEY); current = await settle()
    const original = current.upload.recovery!.original
    current.closure.prepareRecovery(original); current.closure.prepareRecovery(original)
    current = render(); commitHooks()
    expect(current.upload.batch.items).toEqual([])
    current.closure.submit(); current = await settle()
    expect(closeDocumentUpload).toHaveBeenCalledTimes(1)
    expect(vi.mocked(closeDocumentUpload).mock.calls[0]?.slice(0, 3)).toEqual([PROJECT, KEY, csrf])
    if (mode === 'unknown') {
      current.closure.finishRecovery(); current.upload.closeRecovery()
      expect(current.closure.locked()).toBe(true)
      expect(current.upload.canStart()).toBe(false)
      vi.mocked(loadDocumentUploadClosure).mockRejectedValueOnce(new ApiProblemError('private', 404, 'document_upload_closure_not_found'))
      current.closure.check(original); current = await settle()
      expect(current.closure.state?.phase).toBe('unknown')
      current.closure.check(original); current = await settle()
    }
    expect(current.closure.state).toMatchObject({ kind: 'recovery', phase: 'closed' })
    expect(current.upload.batch.items).toEqual([])
    expect(current.upload.canStart()).toBe(false)
    expect(published).not.toHaveBeenCalled()
    current.closure.finishRecovery(); current = render()
    expect(current.upload.canStart()).toBe(true)
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })
  it('cannot use a retained PENDING candidate after it was replaced or closed', async () => {
    vi.mocked(loadDocumentUpload).mockResolvedValue({ project_id: PROJECT, upload_key: KEY,
      created_at: '2026-09-10T00:00:00Z', state: 'PENDING', document: null })
    render(); commitHooks(); let current = render()
    current.upload.recover(KEY); current = await settle()
    const original = current.upload.recovery!.original
    current.upload.closeRecovery(); current.closure.prepareRecovery(original)
    expect(render().closure.state).toBeNull()
    expect(closeDocumentUpload).not.toHaveBeenCalled()
  })
  it('cannot use independent PENDING to bypass an unresolved real batch', async () => {
    vi.mocked(loadDocumentUpload).mockResolvedValue({ project_id: PROJECT, upload_key: KEY,
      created_at: '2026-09-10T00:00:00Z', state: 'PENDING', document: null })
    let current = await unknown()
    const before = current.upload.batch
    current.upload.recover(KEY); current = await settle()
    current.closure.prepareRecovery(current.upload.recovery!.original)
    expect(render().closure.state).toBeNull()
    expect(render().upload.batch).toBe(before)
    expect(closeDocumentUpload).not.toHaveBeenCalled()
  })
  it.each(['submit', 'check'] as const)('closes both older manual query types at valid closure %s dispatch', async (action) => {
    const closureResponse = deferred<DocumentUploadClosureReceipt>()
    const uploadResponse = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUploadClosure).mockReturnValueOnce(closureResponse.promise)
    vi.mocked(loadDocumentUpload).mockReturnValueOnce(uploadResponse.promise)
    vi.mocked(closeDocumentUpload).mockRejectedValueOnce(new TypeError('private'))
    let current = action === 'check' ? await stopping() : await unknown()
    const original = current.upload.batch.items[0]!.original
    if (action === 'submit') { current.closure.prepare(original); current = await settle() }
    current.closure.recovery.lookup(KEY); current.upload.recover(OTHER); current = await settle()
    if (action === 'submit') current.closure.submit()
    else current.closure.check(original)
    expect(vi.mocked(loadDocumentUploadClosure).mock.calls[0]?.[2]?.aborted).toBe(true)
    expect(vi.mocked(loadDocumentUpload).mock.calls[0]?.[2]?.aborted).toBe(true)
    closureResponse.reject(new ApiProblemError('private', 401))
    uploadResponse.reject(new ApiProblemError('private', 401))
    current = await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(current.closure.state?.phase).toBe(action === 'submit' ? 'unknown' : 'closed')
  })
  it('closes manual closure GET synchronously when a new original upload begins', async () => {
    const response = deferred<DocumentUploadClosureReceipt>()
    vi.mocked(loadDocumentUploadClosure).mockReturnValue(response.promise)
    render(); commitHooks(); let current = render()
    current.closure.recovery.lookup(KEY); current = await settle()
    current.upload.start([new File(['new'], 'new.md')])
    expect(vi.mocked(loadDocumentUploadClosure).mock.calls[0]?.[2]?.aborted).toBe(true)
    response.reject(new ApiProblemError('private', 401)); current = await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(current.upload.batch.items[0]?.phase).toBe('published')
    expect(current.closure.recovery.key).toBeNull()
  })
  it('requires confirmation, prevents same-tick repeats, and resumes queued immutable bytes only explicitly', async () => {
    let current = await unknown()
    const [first, queued] = current.upload.batch.items.map((item) => item.original)
    current.closure.prepare(first!); current.closure.prepare(first!)
    expect(closeDocumentUpload).not.toHaveBeenCalled()
    current = render(); commitHooks()
    expect(current.upload.closing).toBe(true)
    current.upload.checkOriginal(); expect(loadDocumentUpload).not.toHaveBeenCalled()
    current.closure.submit(); current.closure.submit(); current = await settle()
    expect(closeDocumentUpload).toHaveBeenCalledTimes(1)
    expect(current.upload.batch.items.map((item) => item.phase)).toEqual(['closed', 'queued'])
    expect(current.upload.canStart()).toBe(false)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
    expect(published).not.toHaveBeenCalled()
    current.upload.continueBatch(); current = await settle()
    expect(vi.mocked(uploadProjectDocument).mock.calls[1]?.slice(0, 3)).toEqual([PROJECT, queued!.uploadKey, queued!.body])
    expect(await queued!.body!.file.text()).toBe('queued')
    expect(current.upload.batch.items.map((item) => item.phase)).toEqual(['closed', 'published'])
  })
  it('cannot stop queued items or clear the original unknown by cancelling preparation', async () => {
    let current = await unknown()
    current.closure.prepare(current.upload.batch.items[1]!.original)
    expect(render().closure.state).toBeNull()
    current.closure.prepare(current.upload.batch.items[0]!.original)
    current = render(); current.closure.cancelPreparation(); current = render()
    expect(current.closure.locked()).toBe(false)
    expect(current.upload.isLocked()).toBe(true)
    expect(current.upload.batch.items[0]?.phase).toBe('unknown')
    expect(closeDocumentUpload).not.toHaveBeenCalled()
  })
  it.each([[409, 'document_upload_already_published'], [404, 'document_upload_not_found'],
    [422, 'validation_error']] as const)('retains upload unknown after known closure refusal %s/%s', async (status, code) => {
    vi.mocked(closeDocumentUpload).mockRejectedValue(new ApiProblemError('private', status, code))
    const current = await stopping()
    expect(current.closure.state?.phase).toBe('refused')
    expect(current.closure.locked()).toBe(false)
    expect(current.upload.closing).toBe(false)
    expect(current.upload.batch.items[0]?.phase).toBe('unknown')
    expect(current.upload.isLocked()).toBe(true)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })
  it.each([[404, 'document_upload_closure_not_found'], [503, 'document_upload_unavailable'],
    [409, 'document_upload_already_published']] as const)('does not release uncertain closure after GET %s/%s', async (status, code) => {
    vi.mocked(closeDocumentUpload).mockRejectedValue(new TypeError('private'))
    vi.mocked(loadDocumentUploadClosure).mockRejectedValueOnce(new ApiProblemError('private', status, code))
    let current = await stopping()
    const original = current.upload.batch.items[0]!.original
    current.closure.check(original); current.closure.check(original); current = await settle()
    expect(loadDocumentUploadClosure).toHaveBeenCalledTimes(1)
    expect(current.closure.state?.phase).toBe('unknown')
    expect(current.closure.locked()).toBe(true)
    current.closure.cancelPreparation(); current.closure.prepare(original); current.closure.submit()
    current.upload.checkOriginal(); current.upload.continueBatch()
    expect(loadDocumentUpload).not.toHaveBeenCalled()
    expect(closeDocumentUpload).toHaveBeenCalledTimes(1)
    current.closure.check(original); current = await settle()
    expect(current.upload.batch.items.map((item) => item.phase)).toEqual(['closed', 'queued'])
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })
  it('a read-only closure receipt cannot adopt or release the real unknown batch', async () => {
    vi.mocked(closeDocumentUpload).mockRejectedValue(new TypeError('private'))
    let current = await stopping()
    const original = current.upload.batch.items[0]!.original
    current.closure.recovery.lookup(original.uploadKey); current = await settle()
    expect(current.closure.recovery.receipt?.publication_state).toBe('CLOSED')
    current.closure.recovery.close(); current = render()
    expect(current.closure.state?.phase).toBe('unknown')
    expect(current.upload.batch.items[0]?.phase).toBe('unknown')
    expect(current.upload.closing).toBe(true)
    expect(current.upload.start([new File(['new'], 'new.md')])).toBe(false)
    current.closure.check(original); current = await settle()
    expect(current.upload.batch.items[0]?.phase).toBe('closed')
  })
  it.each(['cancel', 'timeout', 'deadline'] as const)('%s preserves closure unknown and ignores late POST 401', async (mode) => {
    const response = deferred<DocumentUploadClosureReceipt>()
    vi.mocked(closeDocumentUpload).mockReturnValue(response.promise)
    let current = await stopping()
    if (mode === 'cancel') current.closure.cancel()
    else if (mode === 'timeout') vi.advanceTimersByTime(30_001)
    else vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401))
    current = await settle()
    expect(current.closure.state?.phase).toBe('unknown')
    expect(current.upload.batch.items[0]?.phase).toBe('unknown')
    expect(denied).not.toHaveBeenCalled()
    expect(current.closure.locked()).toBe(true)
  })
  it.each(['cancel', 'timeout', 'deadline'] as const)('%s closes GET before late 401 without clearing uncertain write', async (mode) => {
    vi.mocked(closeDocumentUpload).mockRejectedValue(new TypeError('private'))
    const response = deferred<DocumentUploadClosureReceipt>()
    vi.mocked(loadDocumentUploadClosure).mockReturnValue(response.promise)
    let current = await stopping()
    current.closure.check(current.upload.batch.items[0]!.original); current = await settle()
    if (mode === 'cancel') current.closure.cancelCheck()
    else if (mode === 'timeout') vi.advanceTimersByTime(30_001)
    else vi.spyOn(performance, 'now').mockReturnValue(31_000)
    response.reject(new ApiProblemError('private', 401)); current = await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(current.closure.state?.phase).toBe('unknown')
    expect(current.upload.closing).toBe(true)
  })
  it.each(['POST', 'GET'] as const)('notifies current %s 401 exactly once', async (method) => {
    if (method === 'POST') vi.mocked(closeDocumentUpload).mockRejectedValue(new ApiProblemError('private', 401))
    else vi.mocked(closeDocumentUpload).mockRejectedValue(new TypeError('private'))
    let current = await stopping()
    if (method === 'GET') {
      vi.mocked(loadDocumentUploadClosure).mockRejectedValue(new ApiProblemError('private', 401))
      current.closure.check(current.upload.batch.items[0]!.original); current = await settle()
    }
    expect(denied).toHaveBeenCalledTimes(1)
    expect(denied).toHaveBeenCalledWith({ key: 'sessionExpired' })
    expect(current.upload.canStart()).toBe(false)
    expect(current.closure.readable()).toBe(false)
  })
  it('keeps archived read confirmation but cannot write or continue queued files', async () => {
    vi.mocked(closeDocumentUpload).mockRejectedValue(new TypeError('private'))
    let current = await stopping(); readOnly = true; current = await settle()
    expect(current.closure.writable()).toBe(false)
    current.closure.check(current.upload.batch.items[0]!.original); current = await settle()
    expect(current.upload.batch.items[0]?.phase).toBe('closed')
    expect(current.upload.canContinue()).toBe(false)
    expect(uploadProjectDocument).toHaveBeenCalledTimes(1)
  })
  it('does not accept late closure 200 after parallel current read qualification fails', async () => {
    const response = deferred<DocumentUploadClosureReceipt>()
    vi.mocked(closeDocumentUpload).mockReturnValue(response.promise)
    let current = await stopping()
    accessible = false; response.resolve(receipt(current.upload.batch.items[0]!.original.uploadKey))
    current = await settle()
    expect(current.upload.batch.items[0]?.phase).toBe('unknown')
    expect(current.closure.state?.phase).toBe('unknown')
  })
  it('closes pre-existing upload GET before closure owns the original item', async () => {
    const response = deferred<DocumentUploadRecord>()
    vi.mocked(loadDocumentUpload).mockReturnValue(response.promise)
    let current = await unknown()
    current.upload.checkOriginal(); current = await settle()
    current.closure.prepare(current.upload.batch.items[0]!.original)
    response.reject(new ApiProblemError('private', 401)); current = await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(current.closure.state?.phase).toBe('confirming')
    expect(current.upload.closing).toBe(true)
  })
  it('ignores old owner POST after session remount, without dispatching under the new session', async () => {
    const response = deferred<DocumentUploadClosureReceipt>()
    vi.mocked(closeDocumentUpload).mockReturnValue(response.promise)
    const old = await stopping(); unmountHooks(); csrf = 'new-session'
    render(); commitHooks(); response.reject(new ApiProblemError('private', 401))
    const current = await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(current.upload.batch.items).toEqual([])
    expect(current.closure.state).toBeNull()
    old.closure.submit(); old.closure.check(old.upload.batch.items[0]!.original)
    expect(closeDocumentUpload).toHaveBeenCalledTimes(1)
  })
  it.each(['missing', 'unavailable', 'cancel', 'replace'] as const)('manual %s can end without changing files or retaining a write lock', async (mode) => {
    const response = deferred<DocumentUploadClosureReceipt>()
    if (mode === 'missing') vi.mocked(loadDocumentUploadClosure).mockRejectedValueOnce(new ApiProblemError('private', 404, 'document_upload_closure_not_found'))
    else if (mode === 'unavailable') vi.mocked(loadDocumentUploadClosure).mockRejectedValueOnce(new ApiProblemError('private', 503, 'document_upload_unavailable'))
    else vi.mocked(loadDocumentUploadClosure).mockReturnValueOnce(response.promise)
    render(); commitHooks(); let current = render()
    current.closure.recovery.lookup(KEY); current = await settle()
    if (mode === 'replace') current.closure.recovery.lookup(OTHER)
    else current.closure.recovery.close()
    if (mode === 'cancel' || mode === 'replace') response.reject(new ApiProblemError('private', 401))
    current = await settle()
    expect(denied).not.toHaveBeenCalled()
    expect(current.closure.locked()).toBe(false)
    expect(current.upload.canStart()).toBe(true)
    expect(current.closure.recovery.key).toBe(mode === 'replace' ? OTHER : null)
    expect(closeDocumentUpload).not.toHaveBeenCalled()
    expect(uploadProjectDocument).not.toHaveBeenCalled()
  })
})
