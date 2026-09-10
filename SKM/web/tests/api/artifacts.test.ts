import { afterEach, describe, expect, it, vi } from 'vitest'
import { ARTIFACT_MAX_BYTES, loadRunArtifactContent, loadRunArtifacts, type RunArtifactRecord } from '../../src/api'
import { requestApiBlob } from '../../src/api/http'
import publishedFixture from '../../../contracts/examples/artifact-list.v1.json'

const PROJECT = '00000000-0000-4000-8000-000000000020'
const RUN = '00000000-0000-4000-8000-000000000030'
const TEXT = 'Original attachment\n日本語・中文\n'
const HEADERS = { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'attachment; filename="report.txt"' }

/** Browser と同じ WebCrypto で合成 fixture の原 byte hash を独立に準備する。 */
async function checksum(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return `sha256:${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')}`
}
const TEXT_CHECKSUM = await checksum(new TextEncoder().encode(TEXT))
const EMPTY_CHECKSUM = await checksum(new Uint8Array())
const MAX_CHECKSUM = await checksum(new TextEncoder().encode('x'.repeat(ARTIFACT_MAX_BYTES)))
const INVALID_CHECKSUM = await checksum(new Uint8Array([0xff]))

/** 同じ byte から作る正規公開 metadata。内部 storage locator は持たせない。 */
function record(content: string | Uint8Array = TEXT): RunArtifactRecord {
  return { artifact_ref: `art_${'a'.repeat(32)}`, project_id: PROJECT, run_id: RUN,
    tool_call_id: '00000000-0000-4000-8000-000000000040', evidence_ref: 'ev_fixture',
    path: 'output/report.txt', size_bytes: typeof content === 'string' ? new TextEncoder().encode(content).byteLength : content.byteLength,
    mime_type: 'text/plain', checksum: content.length === 0 ? EMPTY_CHECKSUM : content.length === ARTIFACT_MAX_BYTES
      ? MAX_CHECKSUM : content instanceof Uint8Array && content[0] === 0xff ? INVALID_CHECKSUM : TEXT_CHECKSUM,
    created_at: '2026-09-10T00:00:00Z' }
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('authorized Artifact publication index', () => {
  it('consumes the shared publication example without inventing a producer-specific reference shape', async () => {
    const first = publishedFixture[0]!
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(publishedFixture))))
    const index = await loadRunArtifacts(first.project_id, first.run_id)
    expect(index).toEqual(publishedFixture)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { headers: HEADERS })))
    expect((await loadRunArtifactContent(first.project_id, first.run_id, index[0]!)).size).toBe(0)
  })

  it('accepts exact published records, including empty and maximum-sized artifacts', async () => {
    const values = [record(''), { ...record('x'.repeat(ARTIFACT_MAX_BYTES)), artifact_ref: 'art_second' }]
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(values)))
    vi.stubGlobal('fetch', fetcher)
    expect(await loadRunArtifacts(PROJECT, RUN)).toEqual(values)
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining(`/projects/${PROJECT}/runs/${RUN}/artifacts`),
      expect.objectContaining({ credentials: 'same-origin', cache: 'no-store' }))
  })

  it.each([
    null, {}, { artifacts: [] }, [null], [{ ...record(), internal_key: 'not-public' }],
    [record(), record()], Array.from({ length: 101 }, (_, index) => ({ ...record(), artifact_ref: `art_${index}` })),
    ...Object.keys(record()).map((field) => [Object.fromEntries(Object.entries(record()).filter(([key]) => key !== field))]),
    ...[
      ['artifact_ref', 'https://example.invalid/artifact'], ['artifact_ref', `art_${'a'.repeat(61)}`],
      ['artifact_ref', 'art_'], ['evidence_ref', 'ev_'], ['evidence_ref', 'bad'],
      ['project_id', RUN], ['run_id', PROJECT], ['tool_call_id', 'unknown'], ['tool_call_id', '00000000-0000-0000-0000-000000000000'],
      ['size_bytes', -1], ['size_bytes', 1.5], ['size_bytes', '2'], ['size_bytes', true], ['size_bytes', ARTIFACT_MAX_BYTES + 1],
      ['mime_type', 'text/html'], ['checksum', `sha256:${'A'.repeat(64)}`],
      ['created_at', '2026-09-10T00:00:00'], ['created_at', '2026-02-30T00:00:00Z'],
      ...['/output/a', 'output/../a', 'output/./a', 'output//a', 'output/a\\b', 'workspace/a', 'output/', 'output/a\n', 'output/a\u202e', 'output/a\u00a0'].map((path) => ['path', path]),
    ].map(([field, value]) => [{ ...record(), [field as string]: value }]),
  ])('rejects malformed, mixed-scope or ambiguous indexes: %j', async (value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value))))
    await expect(loadRunArtifacts(PROJECT, RUN)).rejects.toThrow('contract')
  })

  it('does not read an invalid scope or accept an unexpected successful status', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('[]', { status: 202 }))
    vi.stubGlobal('fetch', fetcher)
    await expect(loadRunArtifacts('not-a-project', RUN)).rejects.toThrow('scope')
    expect(fetcher).not.toHaveBeenCalled()
    await expect(loadRunArtifacts(PROJECT, RUN)).rejects.toMatchObject({ status: 202 })
  })
})

describe('original Artifact bytes', () => {
  it.each(['', TEXT, 'x'.repeat(ARTIFACT_MAX_BYTES)])('checks exact bytes and returns a fixed text Blob (case %#)', async (content) => {
    const fetcher = vi.fn().mockResolvedValue(new Response(content, { headers: HEADERS }))
    vi.stubGlobal('fetch', fetcher)
    const value = await loadRunArtifactContent(PROJECT, RUN, record(content))
    expect(await value.text()).toBe(content)
    expect(value.type).toBe('text/plain;charset=utf-8')
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining(`/artifacts/${record().artifact_ref}/content`),
      expect.objectContaining({ redirect: 'error', cache: 'no-store', credentials: 'same-origin' }))
  })

  it.each([204, 206, 202])('does not download an unexpected %s success', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(status === 204 ? null : TEXT, { status, headers: HEADERS })))
    await expect(loadRunArtifactContent(PROJECT, RUN, record())).rejects.toMatchObject({ status })
  })

  it.each(['size', 'hash', 'encoding'])('rejects incorrect %s without returning a Blob', async (kind) => {
    const raw = kind === 'encoding' ? new Uint8Array([0xff]) : new TextEncoder().encode(TEXT)
    const metadata = record(raw)
    if (kind === 'size') metadata.size_bytes += 1
    if (kind === 'hash') metadata.checksum = `sha256:${'a'.repeat(64)}`
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(raw, { headers: HEADERS })))
    await expect(loadRunArtifactContent(PROJECT, RUN, metadata)).rejects.toMatchObject({ code: 'artifact_content_invalid' })
  })

  it.each(Object.keys(HEADERS))('requires the documented %s header before consuming bytes', async (field) => {
    const headers = new Headers(HEADERS)
    headers.delete(field)
    const cancel = vi.fn()
    const pull = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({ pull, cancel }, { highWaterMark: 0 }), { headers })))
    await expect(loadRunArtifactContent(PROJECT, RUN, record())).rejects.toThrow('headers')
    expect(pull).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([401, 403])('does not wait on a hung %s body', async (status) => {
    const pull = vi.fn(), cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({ pull, cancel }, { highWaterMark: 0 }), { status })))
    await expect(loadRunArtifactContent(PROJECT, RUN, record())).rejects.toMatchObject({ status })
    expect(pull).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it.each([undefined, '1'])('counts actual bytes even if Content-Length is %s', async (declared) => {
    const headers = new Headers(HEADERS)
    if (declared) headers.set('Content-Length', declared)
    const cancel = vi.fn()
    let pulls = 0
    const body = new ReadableStream<Uint8Array>({
      pull(controller) { pulls += 1; controller.enqueue(new Uint8Array(pulls === 1 ? ARTIFACT_MAX_BYTES : 1)) }, cancel,
    }, { highWaterMark: 0 })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { headers })))
    await expect(loadRunArtifactContent(PROJECT, RUN, record())).rejects.toMatchObject({ code: 'response_too_large' })
    expect(pulls).toBe(2)
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('keeps error bodies bounded without losing the original status', async () => {
    const cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      pull(controller) { controller.enqueue(new Uint8Array(65_537)) }, cancel,
    }, { highWaterMark: 0 }), { status: 503 })))
    await expect(loadRunArtifactContent(PROJECT, RUN, record())).rejects.toMatchObject({ status: 503, message: 'API returned 503' })
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('freezes the original receipt before fetching and checks cancellation after digest', async () => {
    const metadata = record()
    const controller = new AbortController()
    let release: ((value: Response) => void) | undefined
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>((resolve) => { release = resolve })))
    const pending = loadRunArtifactContent(PROJECT, RUN, metadata, controller.signal)
    metadata.checksum = `sha256:${'a'.repeat(64)}`
    release?.(new Response(TEXT, { headers: HEADERS }))
    expect(await (await pending).text()).toBe(TEXT)
    controller.abort()
    await expect(loadRunArtifactContent(PROJECT, RUN, record(), controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('cancels a stalled actual stream on abort', async () => {
    const controller = new AbortController(), cancel = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel }, { highWaterMark: 0 }), { headers: HEADERS })))
    const pending = requestApiBlob('/artifact', { signal: controller.signal }, { status: 200, maxBytes: 10 })
    await Promise.resolve()
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalledOnce()
  })
})
