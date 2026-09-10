import { afterEach, describe, expect, it, vi } from 'vitest'
import { API_BASE, ApiProblemError, closeDocumentUpload, loadDocumentUploadClosure } from '../../src/api'
import { documentUploadClosureFailure } from '../../src/lib/documentUploadClosure'

const PROJECT = '00000000-0000-4000-8000-000000000020'
const KEY = '00000000-0000-4000-8000-000000000080'
const DOCUMENT = '00000000-0000-4000-8000-000000000090'
const RECEIPT = { project_id: PROJECT, upload_key: KEY, document_id: DOCUMENT,
  closed_at: '2026-09-10T01:02:03.123456+09:00', publication_state: 'CLOSED' }

/** 実共有 HTTP helper に JSON と HTTP status を入力する。 */
function response(value: unknown, status = 200) {
  const mock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>().mockResolvedValue(
    new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', mock)
  return mock
}
afterEach(() => vi.unstubAllGlobals())

describe('original document upload closure contract', () => {
  it.each([200, 201])('accepts exact closure receipt on POST %s with the frozen confirmation and CSRF', async (status) => {
    const fetch = response(RECEIPT, status)
    expect(await closeDocumentUpload(PROJECT, KEY, 'synthetic-csrf')).toEqual(RECEIPT)
    expect(fetch.mock.calls[0]).toEqual([`${API_BASE}/projects/${PROJECT}/document-uploads/${KEY}/closure`,
      expect.objectContaining({ method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRF-Token': 'synthetic-csrf' },
        body: '{"confirmation":"STOP_PUBLICATION"}' })])
  })
  it('reads with no body, no cache and no POST', async () => {
    const fetch = response(RECEIPT)
    expect(await loadDocumentUploadClosure(PROJECT, KEY)).toEqual(RECEIPT)
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({ cache: 'no-store' })
    expect(fetch.mock.calls[0]?.[1]?.body).toBeUndefined()
    expect(fetch.mock.calls[0]?.[1]?.method).toBeUndefined()
  })
  it.each([
    { upload_key: DOCUMENT }, { project_id: DOCUMENT }, { document_id: 'bad' },
    { upload_key: '00000000-0000-0000-0000-000000000000' },
    { document_id: '00000000-0000-0000-0000-000000000000' },
    { closed_at: '2026-09-10T01:02:03' }, { closed_at: '2026-02-30T01:02:03Z' },
    { publication_state: 'PUBLISHED' }, { publication_state: null }, { storage_key: 'private' },
  ])('rejects corrupt, private or cross-original receipt %j', async (changes) => {
    response({ ...RECEIPT, ...changes })
    await expect(loadDocumentUploadClosure(PROJECT, KEY)).rejects.toThrow()
    await expect(closeDocumentUpload(PROJECT, KEY, 'csrf')).rejects.toThrow()
  })
  it.each(Object.keys(RECEIPT))('rejects a missing required %s', async (field) => {
    response(Object.fromEntries(Object.entries(RECEIPT).filter(([key]) => key !== field)))
    await expect(loadDocumentUploadClosure(PROJECT, KEY)).rejects.toThrow()
  })
  it.each([201, 202, 206])('rejects GET success status %s', async (status) => {
    response(RECEIPT, status)
    await expect(loadDocumentUploadClosure(PROJECT, KEY)).rejects.toThrow()
  })
  it.each([202, 206])('rejects ambiguous POST success status %s', async (status) => {
    response(RECEIPT, status)
    await expect(closeDocumentUpload(PROJECT, KEY, 'csrf')).rejects.toThrow()
  })
  it.each(['bad', '00000000-0000-0000-0000-000000000000'])('rejects bad requested identity %s before fetch', async (value) => {
    const fetch = response(RECEIPT)
    await expect(loadDocumentUploadClosure(PROJECT, value)).rejects.toThrow()
    await expect(closeDocumentUpload(value, KEY, 'csrf')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })
  it.each([
    [true, 409, 'document_upload_already_published', 'alreadyPublished'],
    [true, 404, 'document_upload_not_found', 'uploadNotFound'],
    [false, 404, 'document_upload_closure_not_found', 'notFound'],
    [true, 503, 'document_upload_unavailable', 'unknown'],
    [false, 503, 'document_upload_unavailable', 'unavailable'],
    [true, 409, 'document_upload_closed', 'unknown'],
    [false, 401, 'authentication_required', 'sessionExpired'],
    [false, 403, 'csrf_rejected', 'denied'],
    [false, 404, 'project_not_found', 'denied'],
    [true, 409, 'project_archived', 'archived'],
  ] as const)('classifies %s / %s / %s without trusting private text', (mutation, status, code, key) => {
    expect(documentUploadClosureFailure(new ApiProblemError('private detail', status, code), mutation)).toEqual({ key })
  })
})
