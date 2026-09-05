import { describe, expect, it } from 'vitest'

import { RESULT_PREVIEW_LIMIT, splitOverflow } from '../../src/lib/resultOverflow'

/** 指定件数の連番一覧を作る。 */
function items(count: number): number[] {
  return Array.from({ length: count }, (_, index) => index + 1)
}

describe('splitOverflow', () => {
  it('keeps a short list fully visible', () => {
    /** 既定件数までは畳まない。数件の一覧に開閉操作を足すと読む手数だけ増える。 */
    expect(splitOverflow(items(RESULT_PREVIEW_LIMIT))).toEqual({
      visible: items(RESULT_PREVIEW_LIMIT),
      hidden: [],
    })
  })

  it('does not fold when only one item would be hidden', () => {
    /** 開閉行は畳んだ 1 件とほぼ同じ高さを占め、縦が縮まらないまま操作だけ増える。 */
    expect(splitOverflow(items(RESULT_PREVIEW_LIMIT + 1))).toEqual({
      visible: items(RESULT_PREVIEW_LIMIT + 1),
      hidden: [],
    })
  })

  it('folds the tail once the list is long enough to matter', () => {
    const { visible, hidden } = splitOverflow(items(23))

    expect(visible).toEqual(items(RESULT_PREVIEW_LIMIT))
    expect(hidden).toHaveLength(23 - RESULT_PREVIEW_LIMIT)
    // 総数は視認できる分と畳んだ分の合計から復元できる(件数を落とさない)。
    expect(visible.length + hidden.length).toBe(23)
  })

  it('accepts an explicit limit for tighter lists', () => {
    expect(splitOverflow(items(10), 2)).toEqual({ visible: [1, 2], hidden: items(10).slice(2) })
  })

  it('returns an empty split for an empty list', () => {
    expect(splitOverflow([])).toEqual({ visible: [], hidden: [] })
  })
})
