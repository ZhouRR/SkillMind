import { describe, expect, it } from 'vitest'
import { ApiProblemError } from '../../src/api'
import { DOCUMENT_REQUEST_POLICY, documentFailure } from '../../src/lib/documentFeedback'

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
