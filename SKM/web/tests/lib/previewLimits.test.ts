import { describe, expect, it } from 'vitest'
import { DOCUMENT_PREVIEW_MAX_BYTES, previewTextExceedsLimit } from '../../src/lib/previewLimits'

/** UTF-8 admission は画像の byte 定数と同じ境界で多言語・不正 surrogate を扱う。 */
describe('shared preview byte admission', () => {
  it('accepts exactly 20 MB and rejects the next byte', () => {
    expect(DOCUMENT_PREVIEW_MAX_BYTES).toBe(20_000_000)
    expect(previewTextExceedsLimit('a'.repeat(20_000_000))).toBe(false)
    expect(previewTextExceedsLimit('a'.repeat(20_000_001))).toBe(true)
  })
  it.each(['日', 'é', '😀', '\ud800', '\udc00'])('counts UTF-8 bytes for %s like TextEncoder', (unit) => {
    const bytes = new TextEncoder().encode(unit).length
    const count = Math.floor(DOCUMENT_PREVIEW_MAX_BYTES / bytes)
    const source = unit.repeat(count) + 'a'.repeat(DOCUMENT_PREVIEW_MAX_BYTES % bytes)
    expect(previewTextExceedsLimit(source)).toBe(false)
    expect(previewTextExceedsLimit(source + 'a')).toBe(true)
  })
})
