import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  ApiProblemError,
  deleteProjectDocument,
  loadProjectDocuments,
  loadProjectDocument,
  loadProjectDocumentText,
  loadDocumentUpload,
  projectDocumentContentHref,
  uploadProjectDocument,
} from '../../src/api/index'
import { withInferredContentType } from '../../src/api/documents'
import { freezeDocumentUpload } from '../../src/lib/documentUpload'

const CSRF = 's'.repeat(32)
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const DOCUMENT_ID = '00000000-0000-4000-8000-000000000090'
const UPLOAD_KEY = '00000000-0000-4000-8000-000000000080'

/** 選択時の原 multipart を本番 helper で固定し、client 呼出しには key を明示する。 */
function uploadBody(file: File) {
  return freezeDocumentUpload(DOCUMENT.uploaded_by, PROJECT_ID, file).body!
}

/** 保存済み文書 metadata の代表 response。 */
const DOCUMENT = {
  document_id: DOCUMENT_ID,
  project_id: PROJECT_ID,
  folder: 'specs',
  name: 'overview.md',
  size: 2048,
  mime: 'text/markdown',
  checksum: `sha256:${'a'.repeat(64)}`,
  uploaded_by: '00000000-0000-4000-8000-000000000030',
  created_at: '2026-07-10T00:00:00Z',
} as const

/** JSON body を持つ fetch mock を作る。 */
function jsonFetch(body: unknown, status: number) {
  return vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() =>
    Promise.resolve(new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })),
  )
}

afterEach(() => vi.unstubAllGlobals())

describe('Project document API contract', () => {
  it('reads only exact metadata with no cache and no mutation', async () => {
    const mock = jsonFetch(DOCUMENT, 200)
    vi.stubGlobal('fetch', mock)
    expect(await loadProjectDocument(PROJECT_ID, DOCUMENT_ID)).toEqual(DOCUMENT)
    expect(mock.mock.calls[0]?.[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/documents/${DOCUMENT_ID}`)
    expect(mock.mock.calls[0]?.[1]?.cache).toBe('no-store')
    expect(mock.mock.calls[0]?.[1]?.method).toBeUndefined()
  })

  it.each([200, 201, 202, 205, 206])('rejects DELETE success status %s as unconfirmed', async (status) => {
    vi.stubGlobal('fetch', jsonFetch({}, status))
    await expect(deleteProjectDocument(PROJECT_ID, DOCUMENT_ID, CSRF)).rejects.toThrow()
  })

  it.each([
    { size: -1 }, { size: 1.5 }, { size: Number.MAX_SAFE_INTEGER + 1 },
    { document_id: 'wrong' }, { project_id: DOCUMENT_ID }, { document_id: PROJECT_ID },
    { uploaded_by: 'wrong' }, { created_at: '2026-01-01T00:00:00' },
    { checksum: 'sha256:wrong' }, { name: '' }, { name: 'x'.repeat(201) },
    { mime: '' }, { folder: 'x'.repeat(201) }, { storage_key: 'must-not-be-public' },
  ])('rejects corrupt or cross-target metadata %j', async (changes) => {
    vi.stubGlobal('fetch', jsonFetch({ ...DOCUMENT, ...changes }, 200))
    await expect(loadProjectDocument(PROJECT_ID, DOCUMENT_ID)).rejects.toThrow()
  })

  it('does not accept duplicate identities in the current list', async () => {
    vi.stubGlobal('fetch', jsonFetch({ documents: [DOCUMENT, DOCUMENT] }, 200))
    await expect(loadProjectDocuments(PROJECT_ID)).rejects.toThrow('duplicate')
  })

  it('lists project documents and enforces the record contract', async () => {
    const fetchMock = jsonFetch({ documents: [DOCUMENT] }, 200)
    vi.stubGlobal('fetch', fetchMock)

    const documents = await loadProjectDocuments(PROJECT_ID)

    expect(documents).toHaveLength(1)
    expect(documents[0]?.name).toBe('overview.md')
    expect(documents[0]?.size).toBe(2048)
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/documents`)
    expect(call[1]?.method ?? 'GET').toBe('GET')
  })

  it('rejects a document list whose record drops a required field', async () => {
    const { size: _size, ...withoutSize } = DOCUMENT
    vi.stubGlobal('fetch', jsonFetch({ documents: [withoutSize] }, 200))

    await expect(loadProjectDocuments(PROJECT_ID))
      .rejects.toThrow('Document response did not match its contract')
  })

  it('uploads a directory file by splitting webkitRelativePath into folder and name', async () => {
    const fetchMock = jsonFetch({ ...DOCUMENT, size: 11 }, 201)
    vi.stubGlobal('fetch', fetchMock)

    const file = new File(['# Overview\n'], 'overview.md', { type: 'text/markdown' })
    // Browser の目録 upload と同じく basename の name と相対 path の webkitRelativePath を併せ持つ。
    Object.defineProperty(file, 'webkitRelativePath', { value: 'specs/overview.md' })
    await uploadProjectDocument(PROJECT_ID, UPLOAD_KEY, uploadBody(file), CSRF)

    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/documents`)
    expect(call[1]?.method).toBe('POST')
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF, 'Idempotency-Key': UPLOAD_KEY })
    expect(call[1]?.cache).toBe('no-store')
    // Content-Type は設定しない。undici/browser が multipart boundary を付与する。
    expect(call[1]?.headers).not.toHaveProperty('Content-Type')
    const body = call[1]?.body as FormData
    expect([...body.keys()]).toEqual(['file', 'folder'])
    expect(body.getAll('file')).toHaveLength(1)
    expect(body.getAll('folder')).toHaveLength(1)
    // name は単一 segment に落とし、親 path は folder field で渡す。
    expect((body.get('file') as File).name).toBe('overview.md')
    expect(body.get('folder')).toBe('specs')
  })

  it('uploads a plain file selection with an empty root folder', async () => {
    const fetchMock = jsonFetch({ ...DOCUMENT, folder: '', size: 1 }, 201)
    vi.stubGlobal('fetch', fetchMock)

    await uploadProjectDocument(PROJECT_ID, UPLOAD_KEY, uploadBody(new File(['x'], 'note.txt', { type: 'text/plain' })), CSRF)

    const body = fetchMock.mock.calls[0]?.[1]?.body as FormData
    expect((body.get('file') as File).name).toBe('note.txt')
    expect(body.get('folder')).toBe('')
  })

  it.each([
    [413, 'document_upload_too_large'],
    [422, 'invalid_document_upload'],
    [503, 'document_storage_unavailable'],
  ] as const)('preserves upload errors %s/%s without retrying', async (status, code) => {
    const mock = jsonFetch({ status, code, detail: 'Private upload internals' }, status)
    vi.stubGlobal('fetch', mock)
    const request = uploadProjectDocument(PROJECT_ID, UPLOAD_KEY, uploadBody(new File(['content'], 'note.txt')), CSRF)
    await expect(request).rejects.toBeInstanceOf(ApiProblemError)
    await expect(request).rejects.toMatchObject({ status, code })
    expect(mock).toHaveBeenCalledTimes(1)
  })

  it.each([200, 202, 206])('does not accept upload status %s as publication', async (status) => {
    const mock = jsonFetch(DOCUMENT, status)
    vi.stubGlobal('fetch', mock)
    await expect(uploadProjectDocument(PROJECT_ID, UPLOAD_KEY, uploadBody(new File(['content'], 'note.txt')), CSRF))
      .rejects.toMatchObject({ status })
    expect(mock).toHaveBeenCalledTimes(1)
  })

  it('deletes a document with CSRF and no request body', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() =>
      Promise.resolve(new Response(null, { status: 204 })),
    )
    vi.stubGlobal('fetch', fetchMock)

    await deleteProjectDocument(PROJECT_ID, DOCUMENT_ID, CSRF)

    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/documents/${DOCUMENT_ID}`)
    expect(call[1]?.method).toBe('DELETE')
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
    expect(call[1]?.body).toBeUndefined()
  })

  it('builds a same-origin content href for downloads', () => {
    expect(projectDocumentContentHref(PROJECT_ID, DOCUMENT_ID))
      .toBe(`${API_BASE}/projects/${PROJECT_ID}/documents/${DOCUMENT_ID}/content`)
  })

  it('loads raw document text for previews and converts failures to problems', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(
      new Response('# 正文', { status: 200, headers: { 'Content-Type': 'text/markdown' } }),
    ))
    await expect(loadProjectDocumentText(PROJECT_ID, DOCUMENT_ID)).resolves.toBe('# 正文')

    vi.stubGlobal('fetch', jsonFetch({ detail: 'Document not found', code: 'document_not_found' }, 404))
    await expect(loadProjectDocumentText(PROJECT_ID, DOCUMENT_ID))
      .rejects.toThrow('Document not found')
  })
})

describe('withInferredContentType', () => {
  it('fills the MIME for extensions the OS leaves empty and keeps everything else', () => {
    // Windows は .md に MIME を登録しないことが多く、空 type のまま送ると allowlist で 422 になる。
    const emptyMd = withInferredContentType(new File(['x'], 'guide.md', { type: '' }), 'guide.md')
    expect(emptyMd.type).toBe('text/markdown')

    const declared = new File(['x'], 'guide.md', { type: 'text/x-web-markdown' })
    expect(withInferredContentType(declared, 'guide.md')).toBe(declared)

    const unknown = new File(['x'], 'data.bin', { type: '' })
    expect(withInferredContentType(unknown, 'data.bin')).toBe(unknown)
  })
})

describe('original document upload receipts', () => {
  const identity = { upload_key: UPLOAD_KEY, project_id: PROJECT_ID, created_at: DOCUMENT.created_at }
  const published = { ...identity, state: 'PUBLISHED', document: DOCUMENT }

  it.each(['PENDING', 'PUBLISHED'])('reads only the original key in state %s', async (state) => {
    const record = { ...identity, state, document: state === 'PENDING' ? null : DOCUMENT }
    const mock = jsonFetch(record, 200)
    vi.stubGlobal('fetch', mock)
    expect(await loadDocumentUpload(PROJECT_ID, UPLOAD_KEY)).toEqual(record)
    expect(mock).toHaveBeenCalledTimes(1)
    expect(mock.mock.calls[0]?.[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/document-uploads/${UPLOAD_KEY}`)
    expect(mock.mock.calls[0]?.[1]).toMatchObject({ cache: 'no-store', credentials: 'same-origin' })
    expect(mock.mock.calls[0]?.[1]?.method).toBeUndefined()
  })

  it.each([
    { upload_key: DOCUMENT_ID }, { project_id: DOCUMENT_ID }, { upload_key: 'bad' },
    { created_at: '2026-01-01' }, { created_at: '2026-02-30T00:00:00Z' },
    { state: 'DONE' }, { state: 'PENDING' }, { document: null },
    { document: { ...DOCUMENT, project_id: DOCUMENT_ID } },
    { document: { ...DOCUMENT, storage_key: 'private' } },
    { document: { ...DOCUMENT, size: 1.5 } }, { storage_namespace_id: 'private' },
    { document: { ...DOCUMENT, created_at: '2026-07-09T23:59:59.999999Z' } },
    { document: undefined }, { created_at: undefined },
  ])('rejects invalid or cross-original receipt %j', async (changes) => {
    vi.stubGlobal('fetch', jsonFetch({ ...published, ...changes }, 200))
    await expect(loadDocumentUpload(PROJECT_ID, UPLOAD_KEY)).rejects.toThrow()
  })

  it.each([201, 202, 206])('does not accept receipt status %s', async (status) => {
    vi.stubGlobal('fetch', jsonFetch(published, status))
    await expect(loadDocumentUpload(PROJECT_ID, UPLOAD_KEY)).rejects.toMatchObject({ status })
  })

  it.each(['', 'bad', '00000000-0000-0000-0000-000000000000'])('never generates or substitutes invalid key %s', async (key) => {
    const mock = jsonFetch(published, 200)
    vi.stubGlobal('fetch', mock)
    vi.stubGlobal('crypto', undefined)
    const body = { file: new File(['fixed'], 'note.txt', { type: 'text/plain' }), name: 'note.txt', folder: '' }
    await expect(uploadProjectDocument(PROJECT_ID, key, body, CSRF)).rejects.toThrow('key')
    await expect(loadDocumentUpload(PROJECT_ID, key)).rejects.toThrow('key')
    expect(mock).not.toHaveBeenCalled()
  })

  it('does not need randomness or fetch current directory to accept an original published receipt', async () => {
    const mock = jsonFetch(published, 200)
    vi.stubGlobal('fetch', mock)
    vi.stubGlobal('crypto', undefined)
    expect(await loadDocumentUpload(PROJECT_ID, UPLOAD_KEY)).toEqual(published)
    expect(mock).toHaveBeenCalledTimes(1)
  })

  it('rejects a valid but different-size document as an unconfirmed original POST', async () => {
    const mock = jsonFetch(DOCUMENT, 201)
    vi.stubGlobal('fetch', mock)
    await expect(uploadProjectDocument(PROJECT_ID, UPLOAD_KEY,
      { name: 'overview.md', folder: 'specs', file: new File(['short'], 'overview.md') }, CSRF))
      .rejects.toThrow('size')
    expect(mock).toHaveBeenCalledTimes(1)
  })

  it.each([[404, 'document_upload_not_found'], [503, 'document_upload_unavailable'],
    [409, 'document_upload_pending'], [409, 'document_upload_key_conflict']] as const)(
    'retains exact status/code %s/%s without retry', async (status, code) => {
      const mock = jsonFetch({ status, code, detail: 'private' }, status)
      vi.stubGlobal('fetch', mock)
      await expect(loadDocumentUpload(PROJECT_ID, UPLOAD_KEY)).rejects.toMatchObject({ status, code })
      expect(mock).toHaveBeenCalledTimes(1)
    },
  )
})
