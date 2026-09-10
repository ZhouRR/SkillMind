import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError, requestApiJson } from '../../src/api/http'

afterEach(() => vi.unstubAllGlobals())

describe('explicit JSON decoding at the shared HTTP boundary', () => {
  it('passes original UTF-8 text once before any native numeric conversion', async () => {
    const source = '{"数値":[9007199254740993,1.0,-0.0,1e100]}'
    const bytes = new TextEncoder().encode(source)
    const response = new Response(new ReadableStream({
      start(controller) {
        // UTF-8 の途中も分割し、chunk ごとの文字置換を許さない。
        for (let index = 0; index < bytes.length; index += 3) controller.enqueue(bytes.slice(index, index + 3))
        controller.close()
      },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    const nativeJson = vi.spyOn(response, 'json')
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetchMock)
    const decoded = { original: source }
    const decode = vi.fn(() => decoded)
    const validate = vi.fn()
    const controller = new AbortController()
    const result = await requestApiJson('/fixture', { cache: 'no-store', signal: controller.signal }, {
      statuses: [200], decode, validate,
    })
    expect(result).toBe(decoded)
    expect(decode).toHaveBeenCalledExactlyOnceWith(source)
    expect(validate).toHaveBeenCalledExactlyOnceWith(decoded, response)
    expect(nativeJson).not.toHaveBeenCalled()
    expect(response.bodyUsed).toBe(true)
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/fixture', {
      cache: 'no-store', signal: controller.signal, credentials: 'same-origin', headers: { Accept: 'application/json' },
    })
  })

  it.each(['', 'null', 'false', '0'])('does not skip an explicitly decoded primitive: %s', async (source) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(source)))
    const decode = vi.fn(() => null)
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, { statuses: [200], decode, validate })).resolves.toBeNull()
    expect(decode).toHaveBeenCalledExactlyOnceWith(source)
    expect(validate.mock.calls[0]?.[0]).toBeNull()
  })

  it('leaves ordinary JSON consumers on their original decoder', async () => {
    const response = new Response('{"count":3}')
    const nativeJson = vi.spyOn(response, 'json')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
    await expect(requestApiJson('/fixture', {}, 200)).resolves.toEqual({ count: 3 })
    expect(nativeJson).toHaveBeenCalledOnce()
  })

  it('does not impose a source-upload byte limit on generated JSON declarations', async () => {
    // Source upload と生成 Manifest/enum の総量は異なる契約である。
    const source = `{"enum":["${'x'.repeat(5 * 1024 * 1024 + 17)}"]}`
    const response = new Response(source, { headers: { 'Content-Length': String(source.length) } })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
    const decode = vi.fn((text: string) => text.length)
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, { statuses: [200], decode, validate })).resolves.toBe(source.length)
    expect(decode).toHaveBeenCalledOnce()
    expect(validate).toHaveBeenCalledExactlyOnceWith(source.length, response)
  })

  it('rejects an unexpected success status before invoking any decoder or validator', async () => {
    const response = new Response('{}', { status: 202 })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
    const decode = vi.fn()
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, { statuses: [200], decode, validate })).rejects.toMatchObject({ status: 202 })
    expect(decode).not.toHaveBeenCalled()
    expect(validate).not.toHaveBeenCalled()
    expect(response.bodyUsed).toBe(false)
  })

  it.each([401, 403, 409, 429, 503])('keeps Problem handling separate from a success decoder: %s', async (status) => {
    const response = new Response('{"code":"fixture_rejected"}', { status, headers: { 'Retry-After': '15' } })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
    const decode = vi.fn()
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, { statuses: [200], decode, validate })).rejects.toMatchObject({
      name: 'ApiProblemError', status, code: 'fixture_rejected', retryAfterSeconds: 15,
    })
    expect(decode).not.toHaveBeenCalled()
    expect(validate).not.toHaveBeenCalled()
  })

  it('does not relabel a decoder contract rejection as a transport failure', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response('{}')))
    const mismatch = new SyntaxError('Invalid JSON value')
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, {
      statuses: [200], decode: () => { throw mismatch }, validate,
    })).rejects.toBe(mismatch)
    expect(validate).not.toHaveBeenCalled()
  })

  it('does not reinterpret a validator error after custom decoding', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response('{}')))
    const mismatch = new Error('Invalid resource identity')
    const decode = vi.fn(() => ({}))
    await expect(requestApiJson('/fixture', {}, {
      statuses: [200], decode, validate: () => { throw mismatch },
    })).rejects.toBe(mismatch)
    expect(decode).toHaveBeenCalledOnce()
  })

  it.each([
    { bytes: [34, 0xff, 34] }, { bytes: [0xe4] },
    { bytes: [0xed, 0xa0, 0x80] }, { bytes: [0xc0, 0xaf] },
  ])('rejects damaged UTF-8 $bytes before decoding', async ({ bytes }) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(new Uint8Array(bytes))))
    const decode = vi.fn()
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, { statuses: [200], decode, validate })).rejects.toBeInstanceOf(ApiProblemError)
    expect(decode).not.toHaveBeenCalled()
    expect(validate).not.toHaveBeenCalled()
  })

  it('does not start decoding an old body after its signal was aborted', async () => {
    const controller = new AbortController()
    const response = new Response(new ReadableStream({
      pull(stream) { controller.abort(); stream.enqueue(new TextEncoder().encode('{}')); stream.close() },
    }))
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
    const decode = vi.fn()
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', { signal: controller.signal }, { statuses: [200], decode, validate }))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(decode).not.toHaveBeenCalled()
    expect(validate).not.toHaveBeenCalled()
  })
})
