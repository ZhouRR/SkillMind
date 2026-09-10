import { describe, expect, it } from 'vitest'

import { isApiTimestamp, isUuid } from '../../src/lib/validation'

describe('shared identity and time guards', () => {
  it('checks UUID shape without requiring a particular generation version', () => {
    expect(isUuid('A1234567-1234-1234-ABCD-123456789012')).toBe(true)
    expect(isUuid('me')).toBe(false)
    expect(isUuid(null)).toBe(false)
  })
  it.each(['2024-02-29T23:59:59.123456Z', '2026-09-09T15:00:00+09:00', '2000-02-29T00:00:00z'])('accepts a real timezone timestamp %s', (value) => {
    expect(isApiTimestamp(value)).toBe(true)
  })
  it.each(['2026-02-29T00:00:00Z', '1900-02-29T00:00:00Z', '2026-04-31T00:00:00Z', '2026-09-09', '2026-09-09T24:00:00Z', '2026-09-09T01:00:00+25:00'])('rejects normalized or unzoned dates %s', (value) => {
    expect(isApiTimestamp(value)).toBe(false)
  })
})
