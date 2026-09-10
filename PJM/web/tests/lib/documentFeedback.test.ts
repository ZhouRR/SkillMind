import { describe, expect, it } from 'vitest'
import { ApiProblemError } from '../../src/api'
import { DOCUMENT_REQUEST_POLICY, documentFailure, documentUploadFailure } from '../../src/lib/documentFeedback'
import { MESSAGES } from '../../src/lib/i18n/messages'

describe('document read failure boundaries', () => {
  it.each([
    [200, 'response_too_large', 'previewTooLarge'],
    [409, 'document_content_missing', 'contentMissing'],
    [409, 'document_content_invalid', 'contentInvalid'],
    [503, 'document_storage_unavailable', 'storageUnavailable'],
  ] as const)('classifies %s/%s as read failure only', (status, code, key) => {
    const error = new ApiProblemError('internal detail must not become UI text', status, code)
    expect(documentFailure(error, false)).toEqual({ key })
    expect(documentFailure(error, true)).toEqual({ key: 'unknown' })
    expect(DOCUMENT_REQUEST_POLICY.blocks({ key })).toBe(false)
  })

  it.each([401, 403, 404])('does not let a content-looking code reopen denied access %s', (status) => {
    const reason = documentFailure(new ApiProblemError('hidden', status, 'document_content_missing'), false)
    expect(reason.key).toBe(status === 401 ? 'sessionExpired' : 'denied')
    expect(DOCUMENT_REQUEST_POLICY.blocks(reason)).toBe(true)
  })
})

describe('document upload refusal boundaries', () => {
  it('keeps storage 503 unknown for both writes while reads report unavailable', () => {
    const error = new ApiProblemError('Private storage binding', 503, 'document_storage_unavailable')
    expect(documentUploadFailure(error)).toEqual({ key: 'uploadUnknown' })
    expect(documentFailure(error, true)).toEqual({ key: 'unknown' })
    expect(DOCUMENT_REQUEST_POLICY.blocks(documentFailure(error, true))).toBe(true)
    expect(documentFailure(error, false)).toEqual({ key: 'storageUnavailable' })
  })

  it('recognizes only the exact upload size rejection without releasing DELETE unknown', () => {
    const error = new ApiProblemError('Private upload internals', 413, 'document_upload_too_large')
    expect(documentUploadFailure(error)).toEqual({ key: 'uploadTooLarge' })
    expect(documentFailure(error, true)).toEqual({ key: 'unknown' })
    expect(documentFailure(error, false)).toEqual({ key: 'loadFailed' })
    expect(DOCUMENT_REQUEST_POLICY.blocks(documentFailure(error, true))).toBe(true)
  })

  it.each([
    new ApiProblemError('Private upload internals', 413),
    new ApiProblemError('Private upload internals', 413, 'different_code'),
    new ApiProblemError('Private upload internals', 503, 'document_upload_too_large'),
    new ApiProblemError('Private upload internals', 202, 'document_upload_too_large'),
    new Error('Private upload internals'),
  ])('does not certify publication or rejection from an unrecognized failure', (error) => {
    expect(documentUploadFailure(error)).toEqual({ key: 'uploadUnknown' })
  })

  it.each([
    [422, 'invalid_document_upload', 'invalid'],
    [422, 'invalid_document', 'invalid'],
    [401, 'document_upload_too_large', 'sessionExpired'],
    [403, 'document_upload_too_large', 'denied'],
    [404, 'project_not_found', 'denied'],
    [409, 'project_archived', 'archived'],
  ] as const)('preserves shared qualification and validation refusal %s/%s', (status, code, key) => {
    expect(documentUploadFailure(new ApiProblemError('Private upload internals', status, code)))
      .toEqual({ key })
  })

  it.each(['zh', 'ja', 'en'] as const)('uses controlled upload feedback in %s', (language) => {
    const error = new ApiProblemError('Private upload internals', 413, 'document_upload_too_large')
    const labels = MESSAGES[language].documentsPanel.failures
    expect(labels[documentUploadFailure(error).key]).toBe(labels.uploadTooLarge)
    expect(labels.uploadTooLarge).not.toContain(error.message)
    expect(labels.uploadUnknown).not.toBe(labels.unknown)
    expect(labels.uploadUnknown).not.toContain(error.message)
  })
})
