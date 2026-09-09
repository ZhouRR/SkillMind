import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiProblemError, requestApiEmpty, requestApiJson, requestApiText } from '../../src/api/http'

afterEach(() => vi.unstubAllGlobals())

describe('Problem HTTP metadata', () => {
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
