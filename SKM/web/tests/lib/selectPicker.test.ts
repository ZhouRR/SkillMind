import { afterEach, describe, expect, it, vi } from 'vitest'
import { isOpenSelectPicker } from '../../src/lib/selectPicker'

/** DOM の検出境界だけを確認する。実 picker の keyboard 挙動は browser runner が担う。 */
class FixtureElement extends EventTarget {
  constructor(private readonly select: { matches: (selector: string) => boolean } | null) { super() }
  closest(selector: string) { expect(selector).toBe('select'); return this.select }
}

afterEach(() => { vi.unstubAllGlobals() })

describe('native picker dialog keyboard boundary', () => {
  it.each([true, false])('leaves the event to an open picker only: %s', (open) => {
    vi.stubGlobal('Element', FixtureElement)
    vi.stubGlobal('CSS', { supports: (selector: string) => selector === 'selector(select:open)' })
    const matches = vi.fn((selector: string) => selector === ':open' && open)
    expect(isOpenSelectPicker(new FixtureElement({ matches }))).toBe(open)
    expect(matches).toHaveBeenCalledWith(':open')
    expect(isOpenSelectPicker(new FixtureElement(null))).toBe(false)
    expect(isOpenSelectPicker(null)).toBe(false)
    expect(isOpenSelectPicker(new EventTarget())).toBe(false)
  })
  it('does not query unsupported selectors in fallback browsers', () => {
    vi.stubGlobal('Element', FixtureElement)
    vi.stubGlobal('CSS', { supports: () => false })
    const matches = vi.fn()
    expect(isOpenSelectPicker(new FixtureElement({ matches }))).toBe(false)
    expect(matches).not.toHaveBeenCalled()
    vi.stubGlobal('CSS', undefined)
    expect(isOpenSelectPicker(new FixtureElement({ matches }))).toBe(false)
  })
})
