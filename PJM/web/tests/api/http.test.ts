import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError, requestApiEmpty, requestApiJson, requestApiText } from '../../src/api/http'

afterEach(() => vi.unstubAllGlobals())

describe('Problem HTTP metadata', () => {
  it('checks the requested JSON success status without changing unversioned consumers', async () => {
    const transport = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}', { status: 202 }))
    vi.stubGlobal('fetch', transport)
    await expect(requestApiJson('/fixture', {}, 201)).rejects.toMatchObject({ status: 202 })
    await expect(requestApiJson('/fixture')).resolves.toEqual({})
  })

  it('validates the same parsed JSON and HTTP metadata without rereading the body', async () => {
    const response = new Response('{"idempotent_replay":true}', {
      status: 200, headers: { 'Idempotent-Replay': 'true' },
    })
    const json = vi.spyOn(response, 'json')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response))
    const validate = vi.fn()
    await expect(requestApiJson('/fixture', {}, { statuses: [200, 201], validate }))
      .resolves.toEqual({ idempotent_replay: true })
    expect(validate).toHaveBeenCalledWith({ idempotent_replay: true }, response)
    expect(json).toHaveBeenCalledOnce()
  })

  it('does not relabel a resource contract failure as invalid JSON', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response('{}', { status: 201 })))
    const mismatch = new Error('metadata does not match body')
    await expect(requestApiJson('/fixture', {}, {
      statuses: [200, 201], validate: () => { throw mismatch },
    })).rejects.toBe(mismatch)
  })

  it('does not run the success validator for another status or an HTTP rejection', async () => {
    const validate = vi.fn()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('{}', { status: 202 }))
      .mockResolvedValueOnce(new Response('{"code":"interaction_conflict"}', { status: 409 })))
    await expect(requestApiJson('/fixture', {}, { statuses: [200, 201], validate }))
      .rejects.toMatchObject({ status: 202 })
    await expect(requestApiJson('/fixture', {}, { statuses: [200, 201], validate }))
      .rejects.toMatchObject({ status: 409, code: 'interaction_conflict' })
    expect(validate).not.toHaveBeenCalled()
  })

  for (const [name, request] of [
    ['json', () => requestApiJson('/fixture')],
    ['empty', () => requestApiEmpty('/fixture', { method: 'POST' })],
    ['text', () => requestApiText('/fixture')],
  ] as const) {
    it.each([
      ['1', 1], ['300', 300], ['060', 60], [' 60 ', 60], ['0', 0], ['3600', 3600],
      [null, undefined], ['', undefined], ['-1', undefined], ['1.5', undefined],
      ['1e2', undefined], ['NaN', undefined], ['60, 120', undefined],
      ['Wed, 09 Sep 2026 12:00:00 GMT', undefined], ['9007199254740992', undefined],
    ])(`${name} preserves safe delay-seconds without guessing: %s`, async (header, seconds) => {
      // Header の保持は body の field と別に検証し、拒否を再送しない。
      const headers = new Headers({ 'Content-Type': 'application/problem+json' })
      if (header !== null) headers.set('Retry-After', String(header))
      const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
        code: 'login_rate_limited', detail: 'Please wait.',
      }), { status: 429, headers }))
      vi.stubGlobal('fetch', fetchMock)

      await expect(request()).rejects.toMatchObject({
        name: 'ApiProblemError', status: 429, code: 'login_rate_limited', retryAfterSeconds: seconds,
      })
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    it(`${name} keeps status and wait metadata when the body is not JSON`, async () => {
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response('<html>error</html>', {
        status: 503, headers: { 'Retry-After': '30' },
      })))
      const error: unknown = await request().catch((reason: unknown) => reason)
      expect(error).toBeInstanceOf(ApiProblemError)
      expect(error).toMatchObject({ message: 'API returned 503', status: 503, retryAfterSeconds: 30 })
    })
  }
})
