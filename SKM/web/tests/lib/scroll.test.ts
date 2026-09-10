import { describe, expect, it } from 'vitest'

import { isNearBottom } from '../../src/lib/scroll'

describe('isNearBottom', () => {
  it('is true at the bottom and within the default threshold', () => {
    // 末尾ちょうど (scrollTop = scrollHeight - clientHeight)。
    expect(isNearBottom({ scrollHeight: 1000, scrollTop: 800, clientHeight: 200 })).toBe(true)
    // 末尾から 40px 以内はまだ追随対象。
    expect(isNearBottom({ scrollHeight: 1000, scrollTop: 761, clientHeight: 200 })).toBe(true)
  })

  it('is false when the user scrolled up beyond the threshold', () => {
    expect(isNearBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 200 })).toBe(false)
  })

  it('honors a custom threshold', () => {
    expect(isNearBottom({ scrollHeight: 1000, scrollTop: 700, clientHeight: 200 }, 120)).toBe(true)
  })
})
