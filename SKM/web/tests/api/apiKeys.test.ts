import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApiKey, loadApiKeys, revokeApiKey } from '../../src/api'

/** 合成 key はネットワーク認証に用いず、厳格な公開応答だけを確認する。 */
const key = { id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', name: 'Example', key_prefix: 'skm1.AAAAAAAA', created_by: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', created_at: '2026-01-01T00:00:00Z', last_used_at: null, revoked_at: null }
afterEach(() => vi.unstubAllGlobals())
describe('API key client', () => {
  it('returns a once-only secret and sends the normal browser CSRF', async () => {
    const value = { api_key: key, token: `skm1.${'A'.repeat(43)}` }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(value), { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    expect(await createApiKey('Example', 'browser-only')).toEqual(value)
    const request = fetcher.mock.calls[0]?.[1]
    expect(request?.cache).toBe('no-store')
    expect(new Headers(request?.headers).get('X-CSRF-Token')).toBe('browser-only')
    expect(request?.body).toBe(JSON.stringify({ name: 'Example' }))
  })
  it('rejects unexpected secrets in metadata and mismatched revoke identities', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({ items: [{ ...key, token: 'unexpected' }] })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...key, id: key.created_by, revoked_at: key.created_at }))))
    await expect(loadApiKeys()).rejects.toThrow('contract')
    await expect(revokeApiKey(key.id, 'browser-only')).rejects.toThrow('contract')
  })
  it('never automatically repeats creation after a lost response', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('network'))
    vi.stubGlobal('fetch', fetcher)
    await expect(createApiKey('Example', 'browser-only')).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})
