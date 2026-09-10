import { afterEach, describe, expect, it, vi } from 'vitest'
import { requestApiText } from '../../src/api/http'

afterEach(() => vi.unstubAllGlobals())

/** 自動先読みを止め、境界の直後に cancel し余分な chunk を要求しないことを観察する。 */
function streaming(chunks: Uint8Array[], headers: Record<string, string> = {}, status = 200) {
  const cancel = vi.fn()
  let reads = 0
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = chunks[reads++]
      if (chunk) controller.enqueue(chunk)
      else controller.close()
    }, cancel,
  }, { highWaterMark: 0 })
  const response = new Response(stream, { headers, status })
  vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
  return { cancel, stream, readCount: () => reads }
}

describe('bounded text HTTP contract', () => {
  it.each([null, '1', 'bogus'])(
    'counts actual bytes even without a truthful length: %s', async (length) => {
      const body = streaming([new Uint8Array(3), new Uint8Array(3), new Uint8Array(3)],
        length === null ? {} : { 'Content-Length': length })
      await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 5 }))
        .rejects.toMatchObject({ status: 200, code: 'response_too_large' })
      expect(body.readCount()).toBe(2)
      expect(body.cancel).toHaveBeenCalledOnce()
      expect(body.stream.locked).toBe(false)
    },
  )

  it('rejects an oversized declared response before reading a chunk', async () => {
    const body = streaming([new Uint8Array(1)], { 'Content-Length': '9007199254740992' })
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 5 })).rejects.toThrow('limit')
    expect(body.readCount()).toBe(0)
    expect(body.cancel).toHaveBeenCalledOnce()
  })

  it('decodes a split UTF-8 character only after counting all original bytes', async () => {
    const bytes = new TextEncoder().encode('中文')
    const body = streaming([bytes.slice(0, 2), bytes.slice(2, 4), bytes.slice(4)])
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 6 })).resolves.toBe('中文')
    expect(body.stream.locked).toBe(false)
  })

  it('does not substitute replacement characters for malformed UTF-8', async () => {
    const body = streaming([Uint8Array.of(0xc0, 0xaf)])
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 2 })).rejects.toThrow()
    expect(body.cancel).toHaveBeenCalledOnce()
  })

  it('accepts an actually empty file at the zero byte limit', async () => {
    streaming([])
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 0 })).resolves.toBe('')
  })

  it.each([201, 202, 206])('does not display an unexpected successful status %s', async (status) => {
    const body = streaming([new Uint8Array(1)], {}, status)
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 5 })).rejects.toMatchObject({ status })
    expect(body.readCount()).toBe(0)
    expect(body.cancel).toHaveBeenCalledOnce()
  })

  it.each([401, 403, 404, 503])('bounds the error body and preserves HTTP %s', async (status) => {
    const body = streaming([new Uint8Array(8), new Uint8Array(8)], { 'Retry-After': '5' }, status)
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 5 }))
      .rejects.toMatchObject({ status, code: undefined, retryAfterSeconds: 5, message: `API returned ${status}` })
    expect(body.readCount()).toBe(status === 401 || status === 403 ? 0 : 1)
    expect(body.cancel).toHaveBeenCalledOnce()
  })

  it.each([401, 403])('closes known access denial %s without waiting for a stalled body', async (status) => {
    const pull = vi.fn()
    const cancel = vi.fn()
    const stream = new ReadableStream<Uint8Array>({ pull, cancel }, { highWaterMark: 0 })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, { status })))
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 10 }))
      .rejects.toMatchObject({ status })
    expect(pull).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('retains a valid typed Problem without displaying success text', async () => {
    streaming([new TextEncoder().encode('{"code":"document_content_missing","detail":"Unavailable"}')], {}, 409)
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 100 }))
      .rejects.toMatchObject({ status: 409, code: 'document_content_missing' })
  })

  it('aborts a pending stream without returning a partial preview', async () => {
    const cancel = vi.fn()
    const signal = new AbortController()
    let opened: () => void = () => undefined
    const reading = new Promise<void>((resolve) => { opened = resolve })
    const stream = new ReadableStream<Uint8Array>({ pull: () => opened(), cancel }, { highWaterMark: 0 })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(stream)))
    const result = requestApiText('/fixture', { signal: signal.signal }, { status: 200, maxBytes: 10 })
    await reading
    signal.abort()
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalledOnce()
    expect(stream.locked).toBe(false)
  })

  it('does not let a failed cancellation replace the original byte-limit refusal', async () => {
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(new Uint8Array(2)) },
      cancel() { throw new Error('Cancellation failed') },
    }, { highWaterMark: 0 })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(stream)))
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes: 1 }))
      .rejects.toMatchObject({ code: 'response_too_large' })
    expect(stream.locked).toBe(false)
  })

  it.each([-1, 1.5, Number.MAX_SAFE_INTEGER + 1])('rejects invalid limit %s before HTTP', async (maxBytes) => {
    const transport = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', transport)
    await expect(requestApiText('/fixture', {}, { status: 200, maxBytes })).rejects.toThrow(TypeError)
    expect(transport).not.toHaveBeenCalled()
  })
})
