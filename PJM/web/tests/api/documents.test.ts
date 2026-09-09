import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  deleteProjectDocument,
  loadProjectDocuments,
  loadProjectDocument,
  loadProjectDocumentText,
  projectDocumentContentHref,
  uploadProjectDocument,
} from '../../src/api/index'
import { withInferredContentType } from '../../src/api/documents'

const CSRF = 's'.repeat(32)
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const DOCUMENT_ID = '00000000-0000-4000-8000-000000000090'

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
    const fetchMock = jsonFetch(DOCUMENT, 201)
    vi.stubGlobal('fetch', fetchMock)

    const file = new File(['# Overview\n'], 'overview.md', { type: 'text/markdown' })
    // Browser の目録 upload と同じく basename の name と相対 path の webkitRelativePath を併せ持つ。
    Object.defineProperty(file, 'webkitRelativePath', { value: 'specs/overview.md' })
    await uploadProjectDocument(PROJECT_ID, file, CSRF)

    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/documents`)
    expect(call[1]?.method).toBe('POST')
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
    // Content-Type は設定しない。undici/browser が multipart boundary を付与する。
    expect(call[1]?.headers).not.toHaveProperty('Content-Type')
    const body = call[1]?.body as FormData
    // name は単一 segment に落とし、親 path は folder field で渡す。
    expect((body.get('file') as File).name).toBe('overview.md')
    expect(body.get('folder')).toBe('specs')
  })

  it('uploads a plain file selection with an empty root folder', async () => {
    const fetchMock = jsonFetch({ ...DOCUMENT, folder: '' }, 201)
    vi.stubGlobal('fetch', fetchMock)

    await uploadProjectDocument(PROJECT_ID, new File(['x'], 'note.txt', { type: 'text/plain' }), CSRF)

    const body = fetchMock.mock.calls[0]?.[1]?.body as FormData
    expect((body.get('file') as File).name).toBe('note.txt')
    expect(body.get('folder')).toBe('')
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
