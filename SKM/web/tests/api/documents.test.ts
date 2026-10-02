import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  ApiProblemError,
  deleteProjectDocument,
  loadProjectDocuments,
  loadProjectDocument,
  loadProjectDocumentImage,
  loadProjectDocumentText,
  loadDocumentUpload,
  projectDocumentContentHref,
  uploadProjectDocument,
} from '../../src/api/index'
import { withInferredContentType } from '../../src/api/documents'
import { freezeDocumentUpload } from '../../src/lib/documentUpload'
import { DOCUMENT_PREVIEW_MAX_BYTES } from '../../src/lib/documentPreview'

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
  it('submits frozen same-path replacement identity without deleting the original', async () => {
    const response = { ...DOCUMENT, document_id: UPLOAD_KEY, size: 3 }
    const mock = jsonFetch(response, 201)
    vi.stubGlobal('fetch', mock)
    const original = freezeDocumentUpload(DOCUMENT.uploaded_by, PROJECT_ID,
      new File(['new'], DOCUMENT.name), DOCUMENT.folder, [DOCUMENT])
    expect(await uploadProjectDocument(PROJECT_ID, original.uploadKey, original.body!, CSRF)).toEqual(response)
    expect(mock).toHaveBeenCalledTimes(1)
    const request = mock.mock.calls[0]?.[1]
    expect(request?.method).toBe('POST')
    const form = request?.body as FormData
    expect(form.get('replaces_document_id')).toBe(DOCUMENT_ID)
    expect(form.get('expected_checksum')).toBe(DOCUMENT.checksum)
    expect(form.get('folder')).toBe(DOCUMENT.folder)
    expect((form.get('file') as File).name).toBe(DOCUMENT.name)
  })
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

describe('bounded document image previews', () => {
  /** API 境界の署名 fixture。完全な画像 decode は browser 回帰で別に検証する。 */
  const PNG = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])
  const JPEG = new Uint8Array([255, 216, 255, 224])
  const GIF87 = new TextEncoder().encode('GIF87a')
  const GIF89 = new TextEncoder().encode('GIF89a')

  /** 認可済み content endpoint が現在返す添付 metadata を再現する。 */
  function imageHeaders(mime: string | null = 'image/png'): Headers {
    const headers = new Headers({
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Content-Disposition': 'attachment; filename="preview.png"',
    })
    if (mime !== null) headers.set('Content-Type', mime)
    return headers
  }

  it.each([
    ['image/png', PNG], ['IMAGE/PNG', PNG], ['image/jpeg', JPEG],
    ['image/gif', GIF87], ['image/gif', GIF89],
  ])('returns only the declared raster MIME with a matching signature: %s (case %#)', async (mime, bytes) => {
    const controller = new AbortController()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(bytes, { headers: imageHeaders(mime) }))
    vi.stubGlobal('fetch', fetcher)
    const blob = await loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID, controller.signal)
    expect(blob.type).toBe(mime.toLowerCase())
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes)
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(projectDocumentContentHref(PROJECT_ID, DOCUMENT_ID), {
      signal: controller.signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error',
    })
  })

  it.each([
    null, '', 'image/svg+xml', 'text/html', 'application/octet-stream', 'image/webp', 'image/x-png',
    'image/png, image/svg+xml', 'image/png; charset=utf-8',
  ])('rejects unsupported or ambiguous MIME %s before reading the body', async (mime) => {
    const pull = vi.fn(), cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: imageHeaders(mime) })))
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)).rejects.toThrow('headers')
    expect(pull).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each(['Cache-Control', 'X-Content-Type-Options', 'Content-Disposition'])(
    'requires the endpoint security metadata %s before reading the body', async (field) => {
      const headers = imageHeaders()
      headers.delete(field)
      const pull = vi.fn(), cancel = vi.fn()
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(
        new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 }), { headers },
      )))
      await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)).rejects.toThrow('headers')
      expect(pull).not.toHaveBeenCalled()
      expect(cancel).toHaveBeenCalledOnce()
    },
  )

  it.each([
    ['image/png', new Uint8Array()], ['image/png', PNG.slice(0, -1)],
    ['image/png', new TextEncoder().encode('<svg onload="alert(1)"></svg>')],
    ['image/jpeg', new TextEncoder().encode('<html><script>alert(1)</script></html>')],
    ['image/gif', new TextEncoder().encode('not an image')],
    ['image/jpeg', PNG], ['image/png', GIF89], ['image/gif', JPEG],
  ])('rejects empty, truncated or mislabeled raster content before returning a Blob (case %#)', async (mime, bytes) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(bytes, { headers: imageHeaders(mime) })))
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID))
      .rejects.toMatchObject({ status: 409, code: 'document_content_invalid' })
  })

  it('accepts the exact existing preview byte limit', async () => {
    const bytes = new Uint8Array(DOCUMENT_PREVIEW_MAX_BYTES)
    bytes.set(PNG)
    const headers = imageHeaders()
    headers.set('Content-Length', String(bytes.length))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(bytes, { headers })))
    const blob = await loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)
    expect(DOCUMENT_PREVIEW_MAX_BYTES).toBe(1_000_000)
    expect(blob.size).toBe(DOCUMENT_PREVIEW_MAX_BYTES)
  })

  it('rejects a declared oversized response without consuming its stream', async () => {
    const pull = vi.fn(), cancel = vi.fn(), headers = imageHeaders()
    headers.set('Content-Length', String(DOCUMENT_PREVIEW_MAX_BYTES + 1))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(
      new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 }), { headers },
    )))
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)).rejects.toMatchObject({ code: 'response_too_large' })
    expect(pull).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([null, '1', 'invalid'])('bounds actual stream bytes despite Content-Length %s', async (declared) => {
    const headers = imageHeaders(), cancel = vi.fn()
    if (declared !== null) headers.set('Content-Length', declared)
    let pulls = 0
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1
        const bytes = new Uint8Array(pulls === 1 ? DOCUMENT_PREVIEW_MAX_BYTES : 1)
        if (pulls === 1) bytes.set(PNG)
        controller.enqueue(bytes)
      }, cancel,
    }, { highWaterMark: 0 })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers })))
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)).rejects.toMatchObject({ code: 'response_too_large' })
    expect(pulls).toBe(2)
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([201, 202, 204, 206])('rejects unexpected successful status %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(status === 204 ? null : PNG,
      { status, headers: imageHeaders() })))
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)).rejects.toMatchObject({ status })
  })

  it.each([401, 403])('surfaces access status %s without waiting on a stalled body', async (status) => {
    const pull = vi.fn(), cancel = vi.fn()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 }), { status },
    ))
    vi.stubGlobal('fetch', fetcher)
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID)).rejects.toMatchObject({ status })
    expect(pull).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it.each([
    [404, 'document_not_found'], [409, 'document_content_invalid'], [503, 'document_storage_unavailable'],
  ])('preserves HTTP error %s/%s without retrying', async (status, code) => {
    const fetcher = jsonFetch({ status, code, detail: 'Document unavailable' }, status)
    vi.stubGlobal('fetch', fetcher)
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID))
      .rejects.toMatchObject({ name: 'ApiProblemError', status, code, message: 'Document unavailable' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('bounds error bodies while retaining the original failure status', async () => {
    const cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(new Uint8Array(65_537)) }, cancel,
    }, { highWaterMark: 0 }), { status: 503 })))
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID))
      .rejects.toMatchObject({ status: 503, message: 'API returned 503' })
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('does not fetch an already aborted image request', async () => {
    const controller = new AbortController(), fetcher = vi.fn<typeof fetch>()
    controller.abort()
    vi.stubGlobal('fetch', fetcher)
    await expect(loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID, controller.signal))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('cancels a stalled stream when the image request is aborted', async () => {
    const controller = new AbortController(), cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(
      new ReadableStream<Uint8Array>({ cancel }, { highWaterMark: 0 }), { headers: imageHeaders() },
    )))
    const pending = loadProjectDocumentImage(PROJECT_ID, DOCUMENT_ID, controller.signal)
    await Promise.resolve()
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalledOnce()
  })
})

describe('withInferredContentType', () => {
  it.each([
    ['spec.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    ['SPEC.XLS', 'application/vnd.ms-excel'],
  ])('fills the Excel MIME for %s without changing its bytes', async (name, mime) => {
    const source = new File([new Uint8Array([0, 255, 80, 75])], name)
    const inferred = withInferredContentType(source, name)
    expect(inferred.type).toBe(mime)
    expect(inferred.name).toBe(name)
    expect(await inferred.arrayBuffer()).toEqual(await source.arrayBuffer())
  })

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
