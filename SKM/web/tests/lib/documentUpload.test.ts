import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError } from '../../src/api'
import { documentUploadFailure } from '../../src/lib/documentFeedback'
import { DOCUMENT_UPLOAD_POLICY, freezeDocumentUpload, uploadBatchLocked, uploadCounts,
  uploadIsUncertain, type DocumentUploadBatch, type DocumentUploadItem } from '../../src/lib/documentUpload'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { projectDeleteErrorMessage } from '../../src/lib/projectFeedback'

const ACTOR = '00000000-0000-4000-8000-000000000030'
const PROJECT = '00000000-0000-4000-8000-000000000020'

/** 各 phase の集計/門禁を検査するためだけの原 file を持つ item を作る。 */
function item(phase: DocumentUploadItem['phase']): DocumentUploadItem {
  return { original: freezeDocumentUpload(ACTOR, PROJECT, new File(['fixed'], 'note.md')),
    phase, document: null, failure: null }
}

afterEach(() => vi.unstubAllGlobals())

describe('immutable original document uploads', () => {
  it('freezes actual bytes, MIME, directory, key and owner before the original File changes', async () => {
    const input = new Uint8Array([65, 66])
    const file = new File([input], 'note.md')
    Object.defineProperty(file, 'webkitRelativePath', { value: 'dir/note.md', configurable: true })
    const original = freezeDocumentUpload(ACTOR, PROJECT, file)
    input.fill(90)
    Object.defineProperty(file, 'webkitRelativePath', { value: 'changed/other.txt' })
    expect(original).toMatchObject({ actorId: ACTOR, projectId: PROJECT, label: 'dir/note.md' })
    expect(original.body).toMatchObject({ name: 'note.md', folder: 'dir' })
    expect(original.body?.file.type).toBe('text/markdown')
    expect(await original.body?.file.text()).toBe('AB')
    expect(Object.isFrozen(original)).toBe(true)
    expect(Object.isFrozen(original.body)).toBe(true)
    expect(Object.isFrozen(original.body?.file)).toBe(true)
    expect(original.uploadKey).toMatch(/^[0-9a-f-]{36}$/i)
  })

  it('assigns new original identities to explicit reselection even with the same file name', () => {
    const first = freezeDocumentUpload(ACTOR, PROJECT, new File(['old'], 'same.md'))
    const second = freezeDocumentUpload(ACTOR, PROJECT, new File(['new'], 'same.md'))
    expect(first.uploadKey).not.toBe(second.uploadKey)
    expect(first.body?.file).not.toBe(second.body?.file)
  })

  it.each([undefined, { randomUUID: () => '00000000-0000-0000-0000-000000000000' }])(
    'does not substitute a weak or nil identity when crypto fails', (crypto) => {
      vi.stubGlobal('crypto', crypto)
      expect(() => freezeDocumentUpload(ACTOR, PROJECT, new File(['x'], 'note.txt'))).toThrow()
    },
  )

  it('reports refused, unknown and unsent separately instead of calling processed files successful', () => {
    const batch = { paused: true, items: [item('published'), item('refused'), item('unknown'), item('queued'), item('sending'), item('closed')] }
    expect(uploadCounts(batch)).toEqual({ published: 1, refused: 1, unknown: 1, queued: 1, sending: 1, closed: 1, total: 6 })
    expect(uploadBatchLocked(batch)).toBe(true)
  })

  it.each(['queued', 'sending', 'unknown'] as const)('keeps the single write gate closed for %s', (phase) => {
    expect(uploadBatchLocked({ paused: false, items: [item(phase)] })).toBe(true)
  })

  it('requires explicit release after checked publication, unlike normal batch completion', () => {
    const batch: DocumentUploadBatch = { paused: true, items: [item('published')] }
    expect(uploadBatchLocked(batch)).toBe(true)
    expect(uploadBatchLocked({ ...batch, paused: false })).toBe(false)
    expect(uploadBatchLocked({ paused: false, items: [item('refused')] })).toBe(false)
  })

  it.each([[409, 'document_upload_pending', 'uploadPending'],
    [409, 'document_upload_key_conflict', 'uploadKeyConflict'],
    [409, 'document_upload_closed', 'uploadClosed'],
    [503, 'document_upload_unavailable', 'uploadUnknown']] as const)(
    'does not release the write gate for %s/%s', (status, code, key) => {
      const failure = documentUploadFailure(new ApiProblemError('private', status, code))
      expect(failure).toEqual({ key })
      expect(uploadIsUncertain(failure)).toBe(true)
      expect(DOCUMENT_UPLOAD_POLICY.blocks(failure)).toBe(true)
    },
  )

  it('does not mistake a missing original upload lookup for denied Project access', () => {
    const error = new ApiProblemError('private', 404, 'document_upload_not_found')
    expect(documentUploadFailure(error, false)).toEqual({ key: 'uploadNotFound' })
    expect(documentUploadFailure(new ApiProblemError('private', 404, 'project_not_found'), false))
      .toEqual({ key: 'denied' })
    expect(documentUploadFailure(new ApiProblemError('private', 413, 'document_upload_too_large'), false))
      .toEqual({ key: 'loadFailed' })
  })

  it.each(['zh', 'ja', 'en'] as const)('has controlled upload recovery and project audit feedback in %s', (language) => {
    const messages = MESSAGES[language]
    const error = new ApiProblemError('private', 409, 'project_delete_blocked_by_document_uploads')
    expect(projectDeleteErrorMessage(error, messages)).toBe(messages.projectManagement.failures.blockedByDocumentUploads)
    expect(projectDeleteErrorMessage(error, messages)).not.toContain(error.message)
    expect(messages.documentsPanel.upload.phase.unknown).not.toBe(messages.documentsPanel.upload.phase.refused)
    expect(messages.documentsPanel.upload.summary(1, 2, 3, 4, 5)).toContain('1')
    expect(messages.documentsPanel.failures.uploadNotFound).not.toBe(messages.documentsPanel.failures.notFound)
    expect(messages.documentsPanel.upload.recoveryHint.length).toBeGreaterThan(20)
    expect(messages.documentsPanel.closure.documentId).not.toBe(messages.documentsPanel.upload.documentId)
  })
})
