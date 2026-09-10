import { describe, expect, it, vi } from 'vitest'

import { createIdempotencyKey } from '../../src/lib/idempotency'

/** Browser の secure context 差異を再現して idempotency key 生成を検証する。 */
describe('createIdempotencyKey', () => {
  it('uses native randomUUID when the browser exposes it', () => {
    const randomUUID = vi.fn(() => '00000000-0000-4000-8000-000000000001')

    expect(createIdempotencyKey({ randomUUID })).toBe(
      '00000000-0000-4000-8000-000000000001',
    )
    expect(randomUUID).toHaveBeenCalledOnce()
  })

  it('builds an RFC 4122 UUID v4 from getRandomValues on HTTP', () => {
    const getRandomValues = vi.fn((array: Uint8Array<ArrayBuffer>) => {
      array.set(Array.from({ length: 16 }, (_, index) => index))
      return array
    })

    const idempotencyKey = createIdempotencyKey({ getRandomValues })

    expect(idempotencyKey).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f')
    expect(idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
  })

  it('fails clearly when Web Crypto is unavailable', () => {
    expect(() => createIdempotencyKey(null)).toThrow(
      'This browser does not provide secure random number generation',
    )
  })
})
